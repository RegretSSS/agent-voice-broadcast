"""TTS 常驻服务（Win / macOS / Linux 通用）。

- 单 pygame.mixer 实例 -> 天然不重叠
- asyncio.Queue 单 consumer -> 顺序播放
- aiohttp IPC @ 0.0.0.0:48271（绑所有接口，方便 WSL NAT 模式经主机 IP 连入）
- pystray 系统托盘 (暂停/继续/跳过/清空/退出)

启动：
    python tts_daemon.py           # 任意平台，前台调试
    # Windows: pythonw tts_daemon.py   (后台无窗口)
    # macOS/Linux: nohup python3 tts_daemon.py >/dev/null 2>&1 &
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

_PROXY_HANDLER = urllib.request.ProxyHandler({})
_OPENER = urllib.request.build_opener(_PROXY_HANDLER)
urllib.request.install_opener(_OPENER)

import edge_tts
import pygame
from aiohttp import web
from sci_fi_tts import MAX_INTER_SEGMENT_GAP_MS, split_sentences

HOST = "0.0.0.0"  # 绑所有接口，让 WSL (NAT 模式) 经主机 IP 连进来
PORT = 48271
PER_ITEM_TIMEOUT = 300.0  # 单条播报最多 5 分钟，超时放弃继续下条，防 consumer 卡死
PLAYER_SCRIPT = str(HERE / "tts_player.py")  # 独立播放进程，pygame 隔离在子进程里
DING_PATH = str(HERE / "ding.mp3")  # 任务开始提示音 (低沉"叮咚"，启动时 edge-tts 合成固化)


def _default_log_file() -> Path:
    """按平台选合理的日志目录。"""
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home())
        return Path(base) / "tts_daemon.log"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Logs" / "tts_daemon.log"
    # Linux / WSL：遵循 XDG，落到 ~/.local/share/tts-daemon/log
    xdg_state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg_state) / "tts-daemon" / "tts_daemon.log"


LOG_FILE = _default_log_file()
DEDUP_TTL_SECONDS = 1800
START_TIME = time.time()

try:
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
except OSError:
    # 权限或只读文件系统时 fallback 到 home 根
    LOG_FILE = Path.home() / "tts_daemon.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler(LOG_FILE, encoding="utf-8")],
)
log = logging.getLogger("tts_daemon")

# 实际播放发生在 tts_player.py 子进程中；daemon 自身的 pygame.mixer 仅供
# status 接口回填 "playing" 字段用。无音频设备时（WSL2 / 无头 Linux / CI 容器）
# init() 会抛异常，这里 try/except 让 daemon 仍能起服务，status 字段降级为 False。
try:
    if not pygame.mixer.get_init():
        pygame.mixer.init()
except Exception as _e:
    log.warning("pygame.mixer.init() failed (daemon will run without local mixer): %s", _e)


def _mixer_busy() -> bool:
    try:
        return bool(pygame.mixer.music.get_busy())
    except Exception:
        return False


def _mixer_stop() -> None:
    try:
        pygame.mixer.music.stop()
    except Exception:
        pass


def _mixer_unpause() -> None:
    try:
        pygame.mixer.music.unpause()
    except Exception:
        pass

queue: asyncio.Queue = asyncio.Queue()
paused_event = asyncio.Event()
paused_event.set()
skip_requested = False
current_item: Optional[dict[str, Any]] = None
seen_request_ids: dict[str, float] = {}
seen_content_keys: dict[str, float] = {}


def _cleanup_seen_cache(now: Optional[float] = None) -> None:
    current_time = now or time.time()
    expired_request_ids = [
        key for key, ts in seen_request_ids.items()
        if current_time - ts > DEDUP_TTL_SECONDS
    ]
    for key in expired_request_ids:
        seen_request_ids.pop(key, None)

    expired_content_keys = [
        key for key, ts in seen_content_keys.items()
        if current_time - ts > DEDUP_TTL_SECONDS
    ]
    for key in expired_content_keys:
        seen_content_keys.pop(key, None)


def _normalize_text_for_dedup(text: str) -> str:
    return " ".join(text.split()).strip()


def _build_content_key(source: str, text: str) -> str:
    normalized = _normalize_text_for_dedup(text)
    return hashlib.sha1(f"{source}\n{normalized}".encode("utf-8")).hexdigest()


def _remember_played(item: dict[str, Any]) -> None:
    now = time.time()
    request_id = item.get("request_id") or ""
    content_key = item.get("content_key") or ""
    if request_id:
        seen_request_ids[request_id] = now
    if content_key:
        seen_content_keys[content_key] = now
    _cleanup_seen_cache(now)


def _is_duplicate(item: dict[str, Any]) -> bool:
    _cleanup_seen_cache()
    request_id = item.get("request_id") or ""
    content_key = item.get("content_key") or ""
    if request_id and request_id in seen_request_ids:
        return True
    if content_key and content_key in seen_content_keys:
        return True
    if current_item is not None:
        if request_id and request_id == current_item.get("request_id"):
            return True
        if content_key and content_key == current_item.get("content_key"):
            return True
    for queued in list(queue._queue):  # type: ignore[attr-defined]
        if request_id and request_id == queued.get("request_id"):
            return True
        if content_key and content_key == queued.get("content_key"):
            return True
    return False


async def _ensure_ding() -> None:
    """用 edge-tts 合成一个低沉的'叮咚'提示音 mp3 (男声 YunjianNeural)，固化复用。"""
    if os.path.exists(DING_PATH):
        return
    try:
        comm = edge_tts.Communicate("叮咚", "zh-CN-XiaoxiaoNeural")
        await comm.save(DING_PATH)
        log.info("ding sound generated at %s", DING_PATH)
    except Exception as e:
        log.warning("ding generation failed: %s", e)


async def _synthesize(text: str, retries: int = 3, per_attempt_timeout: float = 10.0) -> Optional[str]:
    """合成一句为 mp3。失败重试，最终仍失败返回 None（不抛异常，交给调用方跳过）。

    纯标点/空白句直接跳过（edge-tts 必然失败或 hang）；
    每次 attempt 加超时，防止 edge-tts 偶发 hang 死锁整个 consumer。
    """
    if not any(c.isalnum() for c in text):
        log.info("skip punctuation-only sentence: %r", text[:40])
        return None
    path = ""
    for attempt in range(retries):
        try:
            communicate = edge_tts.Communicate(
                text, "zh-CN-XiaoxiaoNeural", rate="+0%", volume="+0%", pitch="+0Hz"
            )
            fd, path = tempfile.mkstemp(suffix=".mp3")
            os.close(fd)
            await asyncio.wait_for(communicate.save(path), timeout=per_attempt_timeout)
            return path
        except Exception as e:
            if path:
                try:
                    os.unlink(path)
                except OSError:
                    pass
                path = ""
            log.warning(
                "synthesize attempt %d/%d failed (%r): %s",
                attempt + 1, retries, text[:40], e,
            )
            await asyncio.sleep(0.3)
    log.error("synthesize gave up after %d retries: %r", retries, text[:60])
    return None


class _Player:
    """常驻播放进程。mixer 只 init 一次，每句只 load+play（省去每句 ~0.45s 冷启动）。
    卡死时 kill 重启，丢一句继续——隔离特性保留。"""

    def __init__(self):
        self.proc = None

    async def _ensure(self):
        if self.proc and self.proc.returncode is None:
            return
        self.proc = await asyncio.create_subprocess_exec(
            sys.executable, PLAYER_SCRIPT,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    async def play(self, mp3_path: str, timeout: float = 60.0) -> bool:
        await self._ensure()
        try:
            self.proc.stdin.write(mp3_path.encode("utf-8") + b"\n")
            await self.proc.stdin.drain()
            line = await asyncio.wait_for(self.proc.stdout.readline(), timeout=timeout)
            return line.strip() == b"OK"
        except Exception as e:
            log.warning("player stuck (%s), killing & will respawn", type(e).__name__)
            try:
                self.proc.kill()
                await asyncio.wait_for(self.proc.wait(), timeout=5)
            except Exception:
                pass
            self.proc = None
            return False

    async def kill(self) -> None:
        """立即停掉当前播放（kill 子进程）。用于暂停/跳过/清空。"""
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.kill()
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except Exception:
                pass
            self.proc = None

    async def stop(self) -> None:
        """发 STOP 命令停当前播放（不杀进程，避免重生幽灵图标）。"""
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.write(b"STOP\n")
                await self.proc.stdin.drain()
            except Exception:
                pass


_player = _Player()


async def _play_in_subprocess(mp3_path: str, timeout: float = 60.0) -> bool:
    """通过常驻播放进程播放一句 mp3。卡死自动 kill 重启。"""
    return await _player.play(mp3_path, timeout)


async def _play_sentences(sentences: list[str]) -> None:
    """逐句播放，句级预取：播第 i 句时后台合成第 i+1 句，减少句间停顿。
    某句卡死只丢这一句，剩余句子照顺序播完。并发度 = 2 (播放 + 合成下一句)。"""
    global skip_requested
    n = len(sentences)
    if n == 0:
        return

    # 任务开始：先停 2 秒（和上一条隔开），再叮咚提醒，再念正文
    if not skip_requested and os.path.exists(DING_PATH):
        await asyncio.sleep(2.0)
        if not skip_requested:
            t_ding = time.time()
            ding_ok = await _play_in_subprocess(DING_PATH)
            log.info("ding played ok=%s %.2fs", ding_ok, time.time() - t_ding)
    elif not os.path.exists(DING_PATH):
        log.warning("ding.mp3 missing at %s — _ensure_ding should regenerate on next daemon start", DING_PATH)

    current = await _synthesize(sentences[0])
    next_task = asyncio.create_task(_synthesize(sentences[1])) if n > 1 else None

    i = 0
    while i < n:
        await paused_event.wait()
        if skip_requested:
            if next_task:
                next_task.cancel()
            break
        if current is None:
            log.info("sentence %d/%d SKIP (synth=None): %r", i + 1, n, sentences[i][:40])
        else:
            t0 = time.time()
            ok = await _play_in_subprocess(current)
            log.info(
                "sentence %d/%d played ok=%s %.2fs: %r",
                i + 1, n, ok, time.time() - t0, sentences[i][:40],
            )
            if os.path.exists(current):
                try:
                    os.unlink(current)
                except OSError:
                    pass
        if next_task is None:
            break
        # 播放期间下一句通常已合成完，这里几乎不等
        current = await next_task
        i += 1
        next_task = (
            asyncio.create_task(_synthesize(sentences[i + 1]))
            if i + 1 < n
            else None
        )


async def play_consumer() -> None:
    global current_item, skip_requested
    log.info("consumer started")
    while True:
        item = await queue.get()
        if item is None:
            log.info("consumer sentinel received, exiting")
            return
        current_item = item
        await paused_event.wait()
        try:
            text = item["text"]
            sentences = split_sentences(text)
            log.info(
                "dequeued source=%s session_id=%s request_id=%s sentences=%d",
                item.get("source", ""),
                item.get("session_id", ""),
                item.get("request_id", ""),
                len(sentences),
            )
            try:
                await asyncio.wait_for(
                    _play_sentences(sentences), timeout=PER_ITEM_TIMEOUT
                )
            except asyncio.TimeoutError:
                skip_requested = True
                _mixer_stop()
                log.error(
                    "item timed out after %ss, abandoning: %r",
                    PER_ITEM_TIMEOUT, text[:60],
                )
            if skip_requested:
                log.info("skipped mid-queue")
            _remember_played(item)
            skip_requested = False
        except Exception:
            log.exception("play_consumer error")
        finally:
            current_item = None


async def handle_ping(request: web.Request) -> web.Response:
    return web.json_response({"alive": True})


async def handle_health(request: web.Request) -> web.Response:
    _cleanup_seen_cache()
    return web.json_response({
        "alive": True,
        "ready": True,
        "queue_size": queue.qsize(),
        "playing": _mixer_busy(),
        "paused": not paused_event.is_set(),
        "current_source": (current_item or {}).get("source"),
        "current_session_id": (current_item or {}).get("session_id"),
        "uptime_seconds": round(time.time() - START_TIME, 2),
    })


async def handle_say(request: web.Request) -> web.Response:
    global skip_requested
    raw = await request.read()
    try:
        body = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        return web.json_response({"error": "bad json"}, status=400)
    text = (body.get("text") or "").strip()
    source = body.get("source", "")
    session_id = body.get("session_id", "")
    request_id = body.get("request_id", "")
    if text:
        item = {
            "source": source,
            "session_id": session_id,
            "request_id": request_id,
            "text": text,
            "content_key": _build_content_key(source, text),
        }
        if _is_duplicate(item):
            log.info(
                "duplicate ignored source=%s session_id=%s request_id=%s",
                source,
                session_id,
                request_id,
            )
            return web.json_response({
                "queued": False,
                "duplicate": True,
                "queue_size": queue.qsize(),
            })
        await queue.put(item)
        log.info(
            "queued from source=%s session_id=%s request_id=%s queue_size=%d",
            source,
            session_id,
            request_id,
            queue.qsize(),
        )
    return web.json_response({
        "queued": bool(text),
        "duplicate": False,
        "queue_size": queue.qsize(),
    })


async def handle_skip(request: web.Request) -> web.Response:
    global skip_requested
    skip_requested = True
    await _player.stop()
    log.info("skip requested")
    return web.json_response({"skipped": True})


async def handle_clear(request: web.Request) -> web.Response:
    global skip_requested
    skip_requested = True
    while not queue.empty():
        queue.get_nowait()
    await _player.stop()
    log.info("queue cleared")
    return web.json_response({"cleared": True})


async def handle_pause(request: web.Request) -> web.Response:
    paused_event.clear()
    await _player.stop()
    log.info("paused")
    return web.json_response({"paused": True})


async def handle_resume(request: web.Request) -> web.Response:
    paused_event.set()
    _mixer_unpause()
    log.info("resumed")
    return web.json_response({"resumed": True})


async def handle_status(request: web.Request) -> web.Response:
    return web.json_response({
        "queue_size": queue.qsize(),
        "playing": _mixer_busy(),
        "paused": not paused_event.is_set(),
        "current_source": (current_item or {}).get("source"),
        "current_session_id": (current_item or {}).get("session_id"),
    })


async def handle_shutdown(request: web.Request) -> web.Response:
    log.info("shutdown requested")
    await queue.put(None)
    asyncio.create_task(_do_shutdown())
    return web.json_response({"shutting_down": True})


async def _do_shutdown() -> None:
    if _tray_icon is not None:
        try:
            _tray_icon.stop()  # 注销托盘图标，避免幽灵残影
        except Exception:
            pass
    await asyncio.sleep(0.3)  # 给 pystray 时间注销
    os._exit(0)


def create_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/ping", handle_ping)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/say", handle_say)
    app.router.add_post("/skip", handle_skip)
    app.router.add_post("/clear", handle_clear)
    app.router.add_post("/pause", handle_pause)
    app.router.add_post("/resume", handle_resume)
    app.router.add_get("/status", handle_status)
    app.router.add_post("/shutdown", handle_shutdown)
    return app


def _tray_request(path: str) -> None:
    try:
        urllib.request.urlopen(
            f"http://127.0.0.1:{PORT}{path}", data=b"{}", timeout=2
        ).read()
    except Exception as e:
        log.warning("tray request %s failed: %s", path, e)


def _tray_pause(icon, item) -> None:
    _tray_request("/pause")


def _tray_resume(icon, item) -> None:
    _tray_request("/resume")


def _tray_skip(icon, item) -> None:
    _tray_request("/skip")


def _tray_clear(icon, item) -> None:
    _tray_request("/clear")


def _tray_quit(icon, item) -> None:
    icon.stop()
    _tray_request("/shutdown")
    time.sleep(0.3)
    os._exit(0)


def _create_icon_image():
    from PIL import Image, ImageDraw
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.ellipse((4, 4, 60, 60), fill=(30, 200, 255))
    d.polygon((26, 22, 26, 42, 44, 32), fill=(15, 30, 45))
    return img


_tray_icon = None


def run_tray() -> None:
    global _tray_icon
    try:
        import pystray
        icon = pystray.Icon(
            "tts_daemon",
            _create_icon_image(),
            f"TTS Daemon ({PORT})",
            menu=pystray.Menu(
                pystray.MenuItem("暂停", _tray_pause),
                pystray.MenuItem("继续", _tray_resume),
                pystray.MenuItem("跳过当前", _tray_skip),
                pystray.MenuItem("清空队列", _tray_clear),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("退出", _tray_quit),
            ),
        )
        _tray_icon = icon
        icon.run()
    except Exception:
        log.exception("tray failed")


async def main_async() -> None:
    await _ensure_ding()
    consumer_task = asyncio.create_task(play_consumer())
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, HOST, PORT)
    await site.start()
    log.info("daemon listening on %s:%d", HOST, PORT)

    tray_thread = threading.Thread(target=run_tray, daemon=True)
    tray_thread.start()

    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()


def is_port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex((host, port)) == 0


def main() -> None:
    if is_port_in_use("127.0.0.1", PORT):
        print(f"port {PORT} already in use - daemon already running?", file=sys.stderr)
        sys.exit(1)
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
