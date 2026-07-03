"""Claude Code Stop hook 桥（Win / macOS / Linux / WSL 通用）。

stdin JSON -> 解析 transcript -> md_to_speech 清洗 -> POST /say。
若 daemon 未启动，按当前平台自动用合适方式后台拉起 tts_daemon.py。

平台行为：
- Windows: pythonw.exe + DETACHED_PROCESS + CREATE_NO_WINDOW，--source 默认 ClaudeCodeInWindows
- macOS:   start_new_session=True (setsid)，--source 默认 ClaudeCodeInMacOS
- Linux:   start_new_session=True，--source 默认 ClaudeCodeInLinux
- WSL2:    --source 默认仍是 ClaudeCodeInLinux；优先经 Windows 主机 IP
           (resolv.conf nameserver) 连 Windows 端 daemon，连不上再回退本机 Linux daemon

显式传 --source / --host 可覆盖自动推断，做手动调试。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

_PROXY_HANDLER = urllib.request.ProxyHandler({})
_OPENER = urllib.request.build_opener(_PROXY_HANDLER)
urllib.request.install_opener(_OPENER)

PORT = 48271
HERE = Path(__file__).resolve().parent
DAEMON_PATH = HERE / "tts_daemon.py"
STARTUP_WAIT = 1.5
HEALTH_TIMEOUT_SECONDS = 12.0
POST_RETRIES = 4
RETRY_INTERVAL_SECONDS = 0.8

# Windows-only creation flags
DETACHED_PROCESS = 0x00000008
CREATE_NO_WINDOW = 0x08000000

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"
IS_LINUX = sys.platform.startswith("linux")


def _is_wsl() -> bool:
    if not IS_LINUX:
        return False
    try:
        with open("/proc/version", "r", encoding="utf-8") as f:
            return "microsoft" in f.read().lower()
    except OSError:
        return False


IS_WSL = _is_wsl()


def _default_source() -> str:
    if IS_WINDOWS:
        return "ClaudeCodeInWindows"
    if IS_MACOS:
        return "ClaudeCodeInMacOS"
    # Linux / WSL 共用同一身份名（区分由 daemon host 自动处理）
    return "ClaudeCodeInLinux"


def _windows_host_ip() -> str:
    """WSL2 NAT 模式下，/etc/resolv.conf 的 nameserver 通常就是 Windows 主机 IP。"""
    try:
        with open("/etc/resolv.conf", "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.lower().startswith("nameserver"):
                    parts = line.split()
                    if len(parts) >= 2 and not parts[1].startswith("127."):
                        return parts[1]
    except OSError:
        pass
    return "127.0.0.1"


def _resolve_daemon_host() -> str:
    """WSL 优先指向 Windows 主机；其它平台走 loopback。"""
    if IS_WSL:
        return _windows_host_ip()
    return "127.0.0.1"


sys.path.insert(0, str(HERE))


def log(msg: str) -> None:
    print(f"[bridge] {msg}", file=sys.stderr)


def http_get(host: str, path: str, timeout: float = 0.5) -> dict | None:
    try:
        with urllib.request.urlopen(f"http://{host}:{PORT}{path}", timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def http_post(host: str, path: str, payload: dict, timeout: float = 2.0) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://{host}:{PORT}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def is_daemon_alive(host: str) -> bool:
    return http_get(host, "/ping") is not None


def get_daemon_health(host: str, timeout: float = 0.8) -> dict | None:
    return http_get(host, "/health", timeout=timeout)


def is_daemon_ready(host: str) -> bool:
    health = get_daemon_health(host)
    return bool(health and health.get("alive") and health.get("ready"))


def _find_python_interpreter() -> str:
    """Windows 用 pythonw.exe（无控制台窗口），其它平台用当前解释器。"""
    exe = sys.executable
    if not IS_WINDOWS:
        return exe
    base, name = os.path.split(exe)
    stem = os.path.splitext(name)[0].lower()
    if stem in ("python", "python3"):
        candidate = os.path.join(base, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    scripts = os.path.join(base, "Scripts")
    if os.path.isdir(scripts):
        candidate = os.path.join(scripts, "pythonw.exe")
        if os.path.exists(candidate):
            return candidate
    return exe


def start_daemon(host: str) -> bool:
    """按平台后台拉起 tts_daemon.py。WSL 下 host 是 Windows 主机 IP，但 daemon 仍要在 Windows 端启动——
    那种跨边界启动 WSL 自己做不到，只能等用户在 Windows 端首次启动时由 Windows 端 bridge 拉起。
    本函数对 WSL 直接 return False，让上层走重试或回退。"""
    if IS_WSL and host != "127.0.0.1":
        log(f"WSL detected, cannot spawn Windows daemon directly — please start it on Windows side")
        return False

    interpreter = _find_python_interpreter()
    log(f"starting daemon via {interpreter}")
    popen_kwargs: dict = dict(
        cwd=str(HERE),
        close_fds=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    if IS_WINDOWS:
        popen_kwargs["creationflags"] = DETACHED_PROCESS | CREATE_NO_WINDOW
    else:
        # macOS / Linux：新会话脱离 controlling tty，父进程退出后不收 SIGTERM
        popen_kwargs["start_new_session"] = True
    subprocess.Popen([interpreter, str(DAEMON_PATH)], **popen_kwargs)

    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    first = True
    while time.time() < deadline:
        time.sleep(STARTUP_WAIT if first else 0.5)
        first = False
        if is_daemon_ready(host):
            log("daemon is up")
            return True
    log("daemon failed to come up")
    return False


def _flatten_content(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict) and c.get("type") == "text":
                parts.append((c.get("text") or "").strip())
            elif isinstance(c, str):
                parts.append(c.strip())
        return "\n".join(p for p in parts if p)
    return ""


def _is_real_prompt(content) -> bool:
    """user entry 是真实用户输入 (True) 还是 tool_result (False)。"""
    if isinstance(content, str):
        return True
    if isinstance(content, list):
        kinds = [c.get("type") for c in content if isinstance(c, dict)]
        if kinds and all(k == "tool_result" for k in kinds):
            return False
        return True
    return False


def extract_from_transcript(transcript_path: str) -> tuple[str, str]:
    """返回 (最后一条真 user prompt, 最后一个完整 turn 的全部 assistant 可见文本)。

    按真 user prompt (非 tool_result) 切分 turn；turn 内所有 assistant text block
    按出现顺序拼接，跳过 thinking / tool_use / tool_result，避免把工具噪音念出来。
    """
    p = Path(transcript_path)
    if not p.exists():
        return "", ""
    last_prompt = ""
    current = ""
    prev_full = ""
    try:
        with p.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                msg = entry.get("message") or {}
                role = msg.get("role") or entry.get("type")
                content = msg.get("content")
                if role == "user":
                    if _is_real_prompt(content):
                        last_prompt = _flatten_content(content)
                        if current:
                            prev_full = current
                            current = ""
                elif role == "assistant":
                    t = _flatten_content(content)
                    if t:
                        current += ("\n" + t) if current else t
    except Exception:
        return "", ""
    final = current or prev_full
    return last_prompt, final


def build_spoken_text(source: str, task: str, result: str) -> str:
    from md_to_speech import md_to_speech

    task_clean = md_to_speech(task)
    result_clean = md_to_speech(result)

    parts = [f"{source} 报告。"]
    if task_clean:
        parts.append(f"任务：{task_clean}。")
    if result_clean:
        parts.append(f"完成情况：{result_clean}。")
    return "".join(parts)


def build_session_id(source: str, transcript_path: str) -> str:
    raw = f"{source}\n{transcript_path}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def build_request_id(source: str, transcript_path: str, task: str, result: str) -> str:
    raw = "\n".join([source, transcript_path, task.strip(), result.strip()])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def ensure_daemon_ready(host: str) -> bool:
    if is_daemon_ready(host):
        return True
    if not is_daemon_alive(host):
        return start_daemon(host)

    deadline = time.time() + HEALTH_TIMEOUT_SECONDS
    while time.time() < deadline:
        if is_daemon_ready(host):
            return True
        time.sleep(0.5)
    return False


def queue_announcement(host: str, payload: dict) -> dict | None:
    last_error: Exception | None = None
    for attempt in range(POST_RETRIES):
        if not ensure_daemon_ready(host):
            log(f"daemon not ready on attempt {attempt + 1} (host={host})")
            continue
        try:
            return http_post(host, "/say", payload, timeout=3.0)
        except Exception as e:
            last_error = e
            log(f"POST /say attempt {attempt + 1} failed: {e}")
            time.sleep(RETRY_INTERVAL_SECONDS)

    if last_error is not None:
        log(f"giving up after retries: {last_error}")
    return None


def _try_post_with_local_fallback(payload: dict) -> dict | None:
    """先打主 host（WSL 下指 Windows），失败时若主 host 非 loopback 再回退本机 daemon。"""
    primary = _resolve_daemon_host()
    resp = queue_announcement(primary, payload)
    if resp is not None:
        return resp
    if primary != "127.0.0.1":
        log(f"primary host {primary} failed, fallback to local 127.0.0.1")
        return queue_announcement("127.0.0.1", payload)
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--source",
        default=None,
        help="身份标识，如 ClaudeCodeInWindows / ClaudeCodeInMacOS / ClaudeCodeInLinux。"
             "不指定时按平台自动推断（WSL 与原生 Linux 都默认 ClaudeCodeInLinux）。",
    )
    parser.add_argument(
        "--host",
        default=None,
        help="daemon host。默认 WSL 用 Windows 主机 IP，其它平台用 127.0.0.1。",
    )
    args, _ = parser.parse_known_args()

    source = args.source or _default_source()

    try:
        hook_input = json.load(sys.stdin)
    except Exception:
        hook_input = {}

    transcript_path = hook_input.get("transcript_path", "")

    task, result = extract_from_transcript(transcript_path)
    if not task and not result:
        log("no transcript content to announce")
        sys.exit(0)

    text = build_spoken_text(source, task, result)
    payload = {
        "source": source,
        "session_id": build_session_id(source, transcript_path),
        "request_id": build_request_id(source, transcript_path, task, result),
        "text": text,
    }

    if args.host:
        # 用户显式指定了 host，单点打，不做 fallback
        resp = queue_announcement(args.host, payload)
    else:
        resp = _try_post_with_local_fallback(payload)
    if resp is None:
        log("daemon unavailable after retries, skipping announcement")
        sys.exit(0)
    log(f"queued: {resp}")


if __name__ == "__main__":
    main()
