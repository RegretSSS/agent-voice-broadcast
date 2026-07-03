#!/usr/bin/env python3
"""纯 TTS 示例。"""

from sci_fi_tts import PRESET_VOICES, SciFiTTS, TTSConfig, split_sentences


def main() -> None:
    text = (
        "从前，在会发光的云朵尽头，住着一只名叫米粒的小鹿。"
        "它最喜欢在清晨收集露珠，因为每一颗露珠里都藏着一小段天空。"
        "有一天，森林的钟声忽然停了，大家都不知道时间跑到哪里去了。"
        "米粒决定沿着银色小路去寻找答案。"
    )

    print("分句结果:")
    for sentence in split_sentences(text):
        print("-", sentence)

    tts = SciFiTTS(TTSConfig(voice=PRESET_VOICES[0]))
    tts.speak_sync(text)


if __name__ == "__main__":
    main()
