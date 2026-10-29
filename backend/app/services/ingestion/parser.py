"""文本解析：编码探测 → 规范化 → 分章。

设计要点
- 编码：中文小说大量来自 GB18030/TXT，直接 utf-8 解码会整篇乱码或抛错；
  用 charset-normalizer 先猜，猜错再按 GB18030 兜底（GB18030 是 GBK 超集，几乎不会误伤）。
- 规范化后的文本才是唯一事实来源：所有 char_start/char_end 偏移都指向它，
  原文下载也返回规范化版本，保证"前端跳转定位"和"后端切片"永远一致。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from charset_normalizer import from_bytes

# 第X章 / 第X回 / 第X节 / 卷 / Chapter N / 序章 / 楔子 / 尾声
CHAPTER_PATTERNS = [
    re.compile(r"^[ \t　]*(第[零一二三四五六七八九十百千万两0-9]{1,9}[章节回卷篇][^\n]{0,40})[ \t　]*$", re.M),
    re.compile(r"^[ \t　]*((?:Chapter|CHAPTER)\s+[0-9IVXLC]{1,6}[^\n]{0,40})[ \t　]*$", re.M),
    re.compile(r"^[ \t　]*((?:序章|序言|楔子|引子|尾声|后记|终章)[^\n]{0,20})[ \t　]*$", re.M),
]

MIN_CHAPTER_BODY = 200      # 低于此字数视为目录页/误判
FALLBACK_CHAPTER_SIZE = 8000  # 无章节结构时按 8000 字虚拟分章


@dataclass
class ChapterSpan:
    chapter_no: int
    title: str
    char_start: int
    char_end: int
    text: str


@dataclass
class ParseResult:
    text: str
    encoding: str
    chapters: list[ChapterSpan] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)
    clean_stats: dict[str, int] = field(default_factory=dict)

    @property
    def char_count(self) -> int:
        return len(self.text)


def decode_bytes(raw: bytes) -> tuple[str, str]:
    """返回 (文本, 编码名)。"""
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw[3:].decode("utf-8", errors="replace"), "utf-8-sig"

    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        pass

    best = from_bytes(raw[: 2 * 1024 * 1024]).best()
    if best is not None and best.encoding:
        try:
            text = raw.decode(best.encoding)
            # 猜出来的编码如果产生大量替换符，说明猜错了
            if text.count("\ufffd") < len(text) * 0.001:
                return text, best.encoding
        except (UnicodeDecodeError, LookupError):
            pass

    return raw.decode("gb18030", errors="replace"), "gb18030"


def normalize(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\ufeff", "")
    text = text.replace("\x00", "")
    text = re.sub(r"[ \t　]+\n", "\n", text)      # 行尾空白
    text = re.sub(r"\n{3,}", "\n\n", text)        # 连续空行压到 2 行
    return text.strip("\n")


def _find_chapter_heads(text: str) -> list[tuple[int, str]]:
    heads: list[tuple[int, str]] = []
    seen_pos: set[int] = set()
    for pat in CHAPTER_PATTERNS:
        for m in pat.finditer(text):
            pos = m.start(1)
            if any(abs(pos - p) < 3 for p in seen_pos):
                continue
            seen_pos.add(pos)
            heads.append((pos, m.group(1).strip()))
    heads.sort(key=lambda x: x[0])
    return heads


def split_chapters(text: str) -> tuple[list[ChapterSpan], list[dict]]:
    issues: list[dict] = []
    heads = _find_chapter_heads(text)

    if len(heads) < 2:
        issues.append(
            {
                "code": "NO_CHAPTER_STRUCTURE",
                "severity": "warn",
                "detail": f"未识别到章节标题，按每 {FALLBACK_CHAPTER_SIZE} 字虚拟分章",
                "char_start": 0,
                "chapter_no": 0,
            }
        )
        spans: list[ChapterSpan] = []
        for i, start in enumerate(range(0, len(text), FALLBACK_CHAPTER_SIZE), start=1):
            end = min(start + FALLBACK_CHAPTER_SIZE, len(text))
            spans.append(ChapterSpan(i, f"片段 {i}", start, end, text[start:end]))
        return spans, issues

    # 首章标题前的内容（书名页/简介）单独成章，避免直接丢掉
    spans = []
    if heads[0][0] > MIN_CHAPTER_BODY:
        spans.append(ChapterSpan(1, "楔子", 0, heads[0][0], text[0 : heads[0][0]].strip()))

    for idx, (pos, title) in enumerate(heads):
        end = heads[idx + 1][0] if idx + 1 < len(heads) else len(text)
        body = text[pos:end].strip()
        spans.append(ChapterSpan(len(spans) + 1, title, pos, end, body))

    # 过滤空章（目录页典型症状：标题紧挨标题）
    kept: list[ChapterSpan] = []
    for sp in spans:
        if len(sp.text) < MIN_CHAPTER_BODY:
            issues.append(
                {
                    "code": "CHAPTER_TOO_SHORT",
                    "severity": "warn",
                    "detail": f"《{sp.title}》正文仅 {len(sp.text)} 字，疑似目录页或漏切",
                    "char_start": sp.char_start,
                    "chapter_no": sp.chapter_no,
                }
            )
        sp.chapter_no = len(kept) + 1
        kept.append(sp)

    # 超长章（> 3 万字）提示：可能是漏识别了卷内小节
    for sp in kept:
        if len(sp.text) > 30000:
            issues.append(
                {
                    "code": "CHAPTER_TOO_LONG",
                    "severity": "info",
                    "detail": f"《{sp.title}》{len(sp.text)} 字，可能漏切子章节",
                    "char_start": sp.char_start,
                    "chapter_no": sp.chapter_no,
                }
            )
    return kept, issues


def parse(raw: bytes) -> ParseResult:
    text, encoding = decode_bytes(raw)
    normalized = normalize(text)
    # 内容级清洗：在分章前对整篇文本做，保证 char_start/char_end 偏移一致，
    # 且清洗后的文本回写为规范文本（下游切片、前端跳转都基于它）
    from app.services.ingestion import cleaner

    clean_res = cleaner.clean(normalized)
    cleaned = clean_res.text
    chapters, issues = split_chapters(cleaned)
    # 清洗命中情况带进处理报告：每条规则一行汇总，位置标 0（跨章节）
    if clean_res.stats:
        from app.services.ingestion.cleaner import CLEAN_DESC

        for rule, n in clean_res.stats.items():
            issues.append(
                {
                    "code": f"CLEAN_{rule.upper()}",
                    "severity": "info",
                    "detail": f"清洗移除 {CLEAN_DESC.get(rule, rule)} {n} 处",
                    "char_start": 0,
                    "chapter_no": 0,
                }
            )
    if cleaned.count("\ufffd"):
        issues.append(
            {
                "code": "ENCODING_SUSPECT",
                "severity": "error",
                "detail": f"检测到 {cleaned.count(chr(0xFFFD))} 个乱码替换符，编码判定可能错误（{encoding}）",
                "char_start": 0,
                "chapter_no": 0,
            }
        )
    return ParseResult(
        text=cleaned, encoding=encoding, chapters=chapters, issues=issues, clean_stats=clean_res.stats
    )
