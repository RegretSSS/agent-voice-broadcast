"""常驻播放进程。

stdin 协议（每行一条）:
  <mp3路径>  播放该文件，播完回 OK
  STOP       立即停止当前播放（不退进程），仍回 OK
  QUIT       退出进程

子线程读 stdin，主线程播放。播放循环非阻塞检查 stop_flag，
STOP 能立即打断当前句而不杀进程，避免频繁重启产生幽灵托盘图标。
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time

os.environ.setdefault("PYGAME_HIDE_SUPPORT_PROMPT", "1")  # 抑制 banner，避免污染 stdout

import pygame

sys.stdin.reconfigure(encoding="utf-8")
sys.stdout.reconfigure(encoding="utf-8")

# 无音频设备（WSL2 / 无头 Linux / CI）时 init() 会抛错。降级为"无 mixer"
# 状态：player 仍接收路径并立刻回 OK，daemon 推进队列，不阻塞 consumer。
_MIXER_AVAILABLE = False
try:
    pygame.mixer.init()
    _MIXER_AVAILABLE = True
except Exception:
    pass

_path_q: "queue.Queue[str | None]" = queue.Queue()
_stop_flag = threading.Event()
_quit_flag = threading.Event()


def _stdin_loop() -> None:
    for line in sys.stdin:
        cmd = line.strip()
        if cmd == "QUIT":
            _quit_flag.set()
            _path_q.put(None)
            return
        if cmd == "STOP":
            _stop_flag.set()
        elif cmd:
            _path_q.put(cmd)


threading.Thread(target=_stdin_loop, daemon=True).start()

while True:
    mp3 = _path_q.get()
    if mp3 is None:
        break
    _stop_flag.clear()  # 清掉上次残留的 STOP，避免误杀下一句
    if not _MIXER_AVAILABLE:
        # 无音频设备：立刻回 OK，让 daemon 继续推进。
        # 注意：不要在这里删 mp3 —— player 不知道哪条路径是临时句、哪条是
        # 持久缓存（如 ding.mp3）。临时句 mp3 的删除由 daemon 在收到 OK 后做。
        sys.stdout.write("OK\n")
        sys.stdout.flush()
        continue
    try:
        pygame.mixer.music.load(mp3)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            if _stop_flag.is_set() or _quit_flag.is_set():
                pygame.mixer.music.stop()
                break
            time.sleep(0.03)
        try:
            pygame.mixer.music.unload()
        except Exception:
            pass
    except Exception:
        # load/play 出错也回 OK，让 daemon 推进，不让本进程崩
        pass
    sys.stdout.write("OK\n")
    sys.stdout.flush()
