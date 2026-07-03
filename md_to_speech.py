"""Markdown -> TTS 友好文本。

- fenced code block (含语言标识) -> "略过详细代码。"
- inline code (`xxx`) -> 保留 (是关键信息)
- 表格 -> "表头：A、B。共 N 行。第一列为：X、Y。"
  - 行数 > 5 时只报 "表头：...。共 N 行。"
- ## 标题 -> 去掉 #
- **粗体** / *斜体* -> 去掉 *
- [text](url) -> 只保留 text
- ![alt](url) -> 删除
- - / 1. 列表 -> 去掉前缀
"""
from __future__ import annotations

import re

CODE_FENCE_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
TABLE_ROW_RE = re.compile(r"^\s*\|.*\|\s*$")
TABLE_SEP_RE = re.compile(r"^\s*\|[\s\-:|]+\|\s*$")
HEADING_RE = re.compile(r"^#{1,6}\s+", re.MULTILINE)
BOLD_RE = re.compile(r"\*\*([^*]+)\*\*")
ITALIC_RE = re.compile(r"(?<!\*)\*([^*\n]+)\*(?!\*)")
IMAGE_RE = re.compile(r"!\[([^\]]*)\]\([^)]+\)")
LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]+\)")
LIST_RE = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)
NUM_LIST_RE = re.compile(r"^\s*\d+\.\s+", re.MULTILINE)
HR_RE = re.compile(r"^\s*[-*_]{3,}\s*$", re.MULTILINE)


def _parse_table_row(line: str) -> list[str]:
    cells = line.strip().strip("|").split("|")
    return [c.strip() for c in cells]


def _convert_table(lines: list[str], start: int) -> tuple[str, int]:
    rows: list[str] = []
    i = start
    while i < len(lines) and TABLE_ROW_RE.match(lines[i]):
        rows.append(lines[i])
        i += 1

    if len(rows) < 2:
        return "", start + 1

    header = _parse_table_row(rows[0])
    data_rows = [_parse_table_row(r) for r in rows[1:] if not TABLE_SEP_RE.match(r)]

    parts = [f"表头：{'、'.join(header)}。"]
    if len(data_rows) <= 5:
        first_col = "、".join(r[0] for r in data_rows if r)
        if first_col:
            parts.append(f"第一列为：{first_col}。")
    parts.append(f"共 {len(data_rows)} 行。")
    return "".join(parts), i


def md_to_speech(md: str) -> str:
    if not md:
        return ""

    md = CODE_FENCE_RE.sub("略过详细代码。", md)
    md = IMAGE_RE.sub("", md)
    md = LINK_RE.sub(r"\1", md)
    md = re.sub(r"`([^`\n]+)`", r"\1", md)

    lines = md.split("\n")
    out: list[str] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        if TABLE_ROW_RE.match(line):
            converted, new_i = _convert_table(lines, i)
            if converted:
                out.append(converted)
                i = new_i
                continue
        out.append(line)
        i += 1
    md = "\n".join(out)

    md = HEADING_RE.sub("", md)
    md = BOLD_RE.sub(r"\1", md)
    md = ITALIC_RE.sub(r"\1", md)
    md = LIST_RE.sub("", md)
    md = NUM_LIST_RE.sub("", md)
    md = HR_RE.sub("", md)

    md = md.replace("_", " ")  # 下划线 -> 空格，避免 TTS 念"下划线"
    md = re.sub(r"\n{3,}", "\n\n", md)
    md = re.sub(r"[ \t]+", " ", md)
    return md.strip()


if __name__ == "__main__":
    import sys
    print(md_to_speech(sys.stdin.read()))
