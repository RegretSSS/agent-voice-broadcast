# Agent语音播报 · Agent Voice Broadcast

[中文](#中文) · [English](#english)

---

## 中文

一个把 LLM agent（Claude Code 等）的回复自动朗读出来的本地常驻服务。基于 `edge-tts` 流式合成、按句播放，开箱即用，跨平台。

### 功能

- **流式分句播放**：长文本按句切分，合成一句播放一句，最小化首句等待
- **常驻 TTS 服务**：后台 `tts_daemon.py` 监听 `0.0.0.0:48271`，单 `pygame.mixer` 实例 + `asyncio.Queue` 单消费者，天然不重叠、严格 FIFO
- **系统托盘控制**：暂停 / 继续 / 跳过当前 / 清空队列 / 退出（Windows 系统托盘 / macOS 菜单栏 / Linux 取决于桌面环境）
- **Stop hook 自动播报**：Claude Code 每轮回复结束后，自动解析 transcript、清洗 Markdown、合成播报文案并 POST 给 daemon
- **去重**：相同 request_id / 内容哈希 30 分钟内自动丢弃
- **三平台支持**：Windows / macOS / Linux / WSL2 同一份代码

### 安装

```bash
pip install -r requirements.txt
```

macOS（Apple Silicon 上 pygame 需要 SDL2）：

```bash
brew install sdl2 sdl2_mixer
pip install -r requirements.txt
```

Linux / WSL2：

```bash
sudo apt install -y python3-pip libsdl2-mixer-2.0-0 \
                    gir1.2-ayatanaappindicator3-0.1 || true
pip3 install -r requirements.txt
```

### 快速开始

```bash
# 交互模式
python sci_fi_tts.py

# 朗读一段文本（自动走常驻 daemon）
python sci_fi_tts.py say "系统已就绪。"

# 朗读文本文件
python sci_fi_tts.py say-file sample_story.txt

# 列出预设语音
python sci_fi_tts.py list-voices
```

### 自动播报（Claude Code Stop hook）

在你的 Claude Code `settings.json` 里加：

```jsonc
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python \"/path/to/claude_hook_bridge.py\""
        // macOS / Linux 用 "python3 ..."
      }]
    }]
  }
}
```

`--source` 不传时按平台自动推断（`ClaudeCodeInWindows` / `ClaudeCodeInMacOS` / `ClaudeCodeInLinux`）。WSL2 会自动从 `/etc/resolv.conf` 找到 Windows 主机的 daemon 并串上去。

详细配置见 [使用指南.md](使用指南.md)。

### 可用语音

- 中文：`zh-CN-XiaoxiaoNeural` / `zh-CN-YunxiNeural` / `zh-CN-YunyangNeural` / `zh-CN-XiaoyiNeural` / `zh-CN-YunjianNeural`
- 英文：`en-US-JennyNeural` / `en-US-GuyNeural` / `en-GB-SoniaNeural`

### 项目结构

| 文件                    | 作用                                                                 |
|-------------------------|----------------------------------------------------------------------|
| `sci_fi_tts.py`         | 流式分句播放器主入口 + CLI                                           |
| `tts_daemon.py`         | 常驻服务（aiohttp IPC + asyncio.Queue + pystray 托盘）               |
| `tts_player.py`         | 独立播放子进程，pygame 隔离在子进程里，卡死自动重启                  |
| `claude_hook_bridge.py` | Stop hook 桥（解析 transcript → 清洗 Markdown → POST /say）           |
| `md_to_speech.py`       | Markdown → TTS 友好文本（代码块/表格/链接清洗）                      |
| `examples.py`           | API 使用示例                                                         |
| `sample_story.txt`      | 试听用文本                                                           |

### License

MIT

---

## English

A resident local service that automatically speaks out replies from LLM agents (Claude Code, etc.). Streaming synthesis per sentence via `edge-tts`, cross-platform out of the box.

### Features

- **Streaming per-sentence playback**: long text is split by sentence; each sentence is synthesized and played back independently, minimizing time-to-first-audio
- **Resident TTS service**: `tts_daemon.py` listens on `0.0.0.0:48271`, single `pygame.mixer` instance + `asyncio.Queue` single-consumer — naturally non-overlapping, strict FIFO
- **System tray control**: pause / resume / skip-current / clear-queue / quit (Windows system tray, macOS menu bar, Linux depending on desktop environment)
- **Stop hook auto-announce**: after each Claude Code reply, the bridge parses the transcript, cleans Markdown, composes the spoken payload and POSTs it to the daemon
- **Dedup**: identical request_id / content hash auto-dropped within 30 minutes
- **Three platforms**: Windows / macOS / Linux / WSL2 from a single codebase

### Install

```bash
pip install -r requirements.txt
```

macOS (pygame needs SDL2 on Apple Silicon):

```bash
brew install sdl2 sdl2_mixer
pip install -r requirements.txt
```

Linux / WSL2:

```bash
sudo apt install -y python3-pip libsdl2-mixer-2.0-0 \
                    gir1.2-ayatanaappindicator3-0.1 || true
pip3 install -r requirements.txt
```

### Quick Start

```bash
# Interactive mode
python sci_fi_tts.py

# Speak a sentence (auto-routed through the resident daemon)
python sci_fi_tts.py say "System ready."

# Speak a text file
python sci_fi_tts.py say-file sample_story.txt

# List preset voices
python sci_fi_tts.py list-voices
```

### Auto-announce (Claude Code Stop hook)

Add this to your Claude Code `settings.json`:

```jsonc
{
  "hooks": {
    "Stop": [{
      "matcher": "",
      "hooks": [{
        "type": "command",
        "command": "python \"/path/to/claude_hook_bridge.py\""
        // use "python3 ..." on macOS / Linux
      }]
    }]
  }
}
```

If `--source` is omitted it is inferred per platform (`ClaudeCodeInWindows` / `ClaudeCodeInMacOS` / `ClaudeCodeInLinux`). On WSL2 the bridge automatically locates the Windows host's daemon via `/etc/resolv.conf`.

See [使用指南.md](使用指南.md) for details.

### Available Voices

- Chinese: `zh-CN-XiaoxiaoNeural` / `zh-CN-YunxiNeural` / `zh-CN-YunyangNeural` / `zh-CN-XiaoyiNeural` / `zh-CN-YunjianNeural`
- English: `en-US-JennyNeural` / `en-US-GuyNeural` / `en-GB-SoniaNeural`

### Project Layout

| File                    | Purpose                                                                                |
|-------------------------|----------------------------------------------------------------------------------------|
| `sci_fi_tts.py`         | Streaming per-sentence player, main entry + CLI                                       |
| `tts_daemon.py`         | Resident service (aiohttp IPC + asyncio.Queue + pystray tray)                         |
| `tts_player.py`         | Standalone playback subprocess, isolates pygame, auto-restarts when stuck             |
| `claude_hook_bridge.py` | Stop hook bridge (parses transcript → cleans Markdown → POST /say)                    |
| `md_to_speech.py`       | Markdown → TTS-friendly text (code-block / table / link cleaning)                     |
| `examples.py`           | API usage examples                                                                     |
| `sample_story.txt`      | Sample text for audition                                                               |

### License

MIT
