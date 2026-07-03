import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sci_fi_tts import PRESET_VOICES, create_parser, split_sentences


class SciFiTTSTestCase(unittest.TestCase):
    def test_split_sentences(self) -> None:
        text = "你好，世界。今天天气很好！我们出发吧？"
        self.assertEqual(
            split_sentences(text),
            ["你好，世界。", "今天天气很好！", "我们出发吧？"],
        )

    def test_split_sentences_with_length_limit(self) -> None:
        text = "这是一个很长很长的句子，里面有很多内容，需要被拆开，才能更快开始播放。"
        parts = split_sentences(text, max_length=10)
        self.assertTrue(all(len(part) <= 10 for part in parts))

    def test_cli_say_file_arguments(self) -> None:
        parser = create_parser()
        args = parser.parse_args(
            [
                "say-file",
                "sample_story.txt",
                "--voice",
                PRESET_VOICES[0],
            ]
        )

        self.assertEqual(args.command, "say-file")
        self.assertEqual(args.path, "sample_story.txt")
        self.assertEqual(args.voice, PRESET_VOICES[0])


if __name__ == "__main__":
    unittest.main()
