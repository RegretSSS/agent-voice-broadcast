#!/usr/bin/env python3
"""
纯 edge-tts 朗读脚本。

特点:
- 只保留现成 TTS 语音
- 按句切分文本
- 逐句生成，边生成边播放
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import tempfile
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import edge_tts
import pygame

DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 48271

_PROXY_HANDLER = urllib.request.ProxyHandler({})
_OPENER = urllib.request.build_opener(_PROXY_HANDLER)
urllib.request.install_opener(_OPENER)

PRESET_VOICES = [
    "zh-CN-XiaoxiaoNeural",
    "zh-CN-YunxiNeural",
    "zh-CN-YunyangNeural",
    "zh-CN-XiaoyiNeural",
    "zh-CN-YunjianNeural",
    "en-US-JennyNeural",
    "en-US-GuyNeural",
    "en-GB-SoniaNeural",
]

MAJOR_BREAK_RE = re.compile(r"(?<=[。！？!?；;\n])")
MINOR_BREAK_RE = re.compile(r"(?<=[，,、：:])")
STRONG_PUNCTUATION = "。！？!?；;"
MAX_INTER_SEGMENT_GAP_MS = 220


@dataclass
class TTSConfig:
    """TTS 配置。"""

    voice: str = "zh-CN-XiaoxiaoNeural"
    rate: str = "+0%"
    volume: str = "+0%"
    pitch: str = "+0Hz"


def split_sentences(text: str, max_length: int = 80) -> list[str]:
    """按强标点优先切句，并限制单句长度。"""
    normalized = re.sub(r"\s+", " ", text).strip()
    if not normalized:
        return []

    coarse_parts = [part.strip() for part in MAJOR_BREAK_RE.split(normalized) if part.strip()]
    results: list[str] = []

    def append_with_limit(piece: str) -> None:
        remaining = piece.strip()
        while remaining:
            if len(remaining) <= max_length:
                results.append(remaining)
                break
            results.append(remaining[:max_length].strip())
            remaining = remaining[max_length:].strip()

    for part in coarse_parts:
        if len(part) <= max_length:
            results.append(part)
            continue

        sub_parts = [item.strip() for item in MINOR_BREAK_RE.split(part) if item.strip()]
        buffer = ""
        for sub_part in sub_parts:
            piece = sub_part.strip()
            if not piece:
                continue
            if not buffer:
                buffer = piece
                continue
            if len(buffer) + len(piece) <= max_length:
                buffer += piece
            else:
                append_with_limit(buffer)
                buffer = piece

        if buffer:
            append_with_limit(buffer)

    merged_results: list[str] = []
    for part in results:
        if not merged_results:
            merged_results.append(part)
            continue

        # Avoid generating very short fragments that amplify playback gaps.
        if (
            len(part) < 8
            and len(merged_results[-1]) + len(part) <= max_length
            and merged_results[-1][-1] not in STRONG_PUNCTUATION
        ):
            merged_results[-1] += part
        else:
            merged_results.append(part)

    return merged_results


class SciFiTTS:
    """基于 edge-tts 的流式分句播放器。"""

    def __init__(self, config: Optional[TTSConfig] = None):
        self.config = config or TTSConfig()
        self._init_audio()

    def _init_audio(self) -> None:
        if not pygame.mixer.get_init():
            pygame.mixer.init()

    async def _synthesize_sentence_to_file(self, text: str) -> str:
        communicate = edge_tts.Communicate(
            text,
            self.config.voice,
            rate=self.config.rate,
            volume=self.config.volume,
            pitch=self.config.pitch,
        )

        temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".mp3")
        temp_path = temp_file.name
        temp_file.close()

        await communicate.save(temp_path)
        return temp_path

    async def _play_file(self, path: str) -> None:
        pygame.mixer.music.load(path)
        pygame.mixer.music.play()
        while pygame.mixer.music.get_busy():
            await asyncio.sleep(0.05)
        pygame.mixer.music.unload()

    async def speak_stream(self, text: str) -> None:
        sentences = split_sentences(text)
        if not sentences:
            return

        sentence_iter = iter(sentences)
        current_sentence = next(sentence_iter, None)
        if current_sentence is None:
            return

        current_path = await self._synthesize_sentence_to_file(current_sentence)
        next_sentence = next(sentence_iter, None)
        next_task = (
            asyncio.create_task(self._synthesize_sentence_to_file(next_sentence))
            if next_sentence is not None
            else None
        )

        while True:
            play_started_at = time.perf_counter()
            try:
                await self._play_file(current_path)
            finally:
                if os.path.exists(current_path):
                    os.unlink(current_path)

            if next_task is None:
                break

            try:
                current_path = await asyncio.wait_for(
                    next_task,
                    timeout=MAX_INTER_SEGMENT_GAP_MS / 1000,
                )
            except asyncio.TimeoutError:
                # If synthesis falls behind, wait for it rather than dropping audio.
                current_path = await next_task

            next_sentence = next(sentence_iter, None)
            next_task = (
                asyncio.create_task(self._synthesize_sentence_to_file(next_sentence))
                if next_sentence is not None
                else None
            )

            # If the sentence was unusually short, keeping one sentence ahead matters
            # more than starting instantly, so yield briefly for the next task.
            elapsed = time.perf_counter() - play_started_at
            if elapsed < 0.35 and next_task is not None:
                await asyncio.sleep(0.02)

    async def speak(self, text: str) -> None:
        await self.speak_stream(text)

    def speak_sync(self, text: str) -> None:
        asyncio.run(self.speak(text))


def build_config_from_args(args: argparse.Namespace) -> TTSConfig:
    return TTSConfig(
        voice=args.voice,
        rate=args.rate,
        volume=args.volume,
        pitch=args.pitch,
    )


def print_voices() -> None:
    print("可用现成语音:")
    for voice in PRESET_VOICES:
        print(f"- {voice}")


def post_to_daemon(text: str, source: str = "CLI", timeout: float = 1.0) -> bool:
    """把文本丢给常驻 daemon。成功 True，连不上返回 False。"""
    try:
        req = urllib.request.Request(
            f"http://{DAEMON_HOST}:{DAEMON_PORT}/say",
            data=json.dumps({"source": source, "text": text}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=timeout).read()
        return True
    except Exception:
        return False


def interactive_mode() -> None:
    print("纯 TTS 交互模式")
    print("直接输入文本开始朗读，输入 quit 退出。\n")

    tts = SciFiTTS()

    while True:
        try:
            text = input(">>> ").strip()
        except KeyboardInterrupt:
            print("\n已退出。")
            break

        if text.lower() in {"quit", "exit", "q"}:
            break
        if text:
            tts.speak_sync(text)


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="纯 edge-tts 分句朗读器")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list-voices", help="列出可用现成语音")

    say_parser = subparsers.add_parser("say", help="朗读一段文本")
    say_parser.add_argument("text", help="要朗读的文本")
    say_parser.add_argument("--voice", default=TTSConfig.voice, choices=PRESET_VOICES)
    say_parser.add_argument("--rate", default=TTSConfig.rate)
    say_parser.add_argument("--volume", default=TTSConfig.volume)
    say_parser.add_argument("--pitch", default=TTSConfig.pitch)

    file_parser = subparsers.add_parser("say-file", help="朗读文本文件")
    file_parser.add_argument("path", help="文本文件路径")
    file_parser.add_argument("--voice", default=TTSConfig.voice, choices=PRESET_VOICES)
    file_parser.add_argument("--rate", default=TTSConfig.rate)
    file_parser.add_argument("--volume", default=TTSConfig.volume)
    file_parser.add_argument("--pitch", default=TTSConfig.pitch)

    return parser


def main() -> None:
    parser = create_parser()
    args = parser.parse_args()

    if args.command is None:
        interactive_mode()
        return
    if args.command == "list-voices":
        print_voices()
        return
    if args.command == "say":
        if post_to_daemon(args.text, source="CLI"):
            print("(queued via daemon)")
            return
        SciFiTTS(build_config_from_args(args)).speak_sync(args.text)
        return
    if args.command == "say-file":
        text = Path(args.path).read_text(encoding="utf-8")
        if post_to_daemon(text, source="CLI"):
            print("(queued via daemon)")
            return
        SciFiTTS(build_config_from_args(args)).speak_sync(text)
        return

    parser.print_help()


if __name__ == "__main__":
    main()
