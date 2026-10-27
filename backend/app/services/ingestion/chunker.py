"""分层切块：child 用于检索，parent 用于喂 LLM（small-to-big）。

为什么是父子而不是单一粒度：
- 400 字的块向量最准（噪声少、语义集中），但 400 字喂给模型不足以回答"这一章发生了什么"；
- 1400 字的父块语义完整，但直接向量化会稀释主题。
所以命中 child 后回溯 parent 取原文，检索精度和上下文完整度不再互相牺牲。

偏移处理：所有 char_start/char_end 都是相对【整篇规范化文本】的绝对偏移，
前端据此可以直接跳到原文位置；切块内部用 local offset 累加得到。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.services.ingestion.parser import ChapterSpan

# 场景分隔符：*** / --- / 空行 + 时间地点转换
SCENE_SEP = re.compile(r"^\s*(?:\*{3,}|-{3,}|={3,}|※{2,})\s*$", re.M)
SENTENCE_END = re.compile(r"(?<=[。！？…!?])")


@dataclass
class DraftChunk:
    level: int
    chapter_no: int
    seq: int
    title: str
    content: str
    char_start: int
    char_end: int
    token_est: int
    parent_key: str = ""          # 局部标识，落库后替换为真实 parent_id
    extra_issues: list[dict] = field(default_factory=list)


def estimate_tokens(text: str) -> int:
    """中文场景按 1 汉字 ≈ 1 token 估算。

    tiktoken 对中文偏差可达 ±40%，这里不用它；产线可换成 BGE tokenizer 精确计数，
    但 estimator 只需保证"同一口径下相对可比"，用于告警阈值判断。
    """
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cjk
    return cjk + other // 2


def _split_with_boundary(text: str, start: int, size: int, overlap: int) -> list[tuple[str, int, int]]:
    """把 text 从 start 开始切成若干段，尽量落在段落/句子边界。

    返回 [(片段, 绝对起始, 绝对结束)]。
    """
    out: list[tuple[str, int, int]] = []
    n = len(text)
    pos = start
    while pos < n:
        end = min(pos + size, n)
        if end < n:
            window = text[pos:end]
            cut = -1
            # 优先段落边界（占窗口后半段才接受，否则段太长会切出很小的块）
            para = window.rfind("\n\n")
            if para > size * 0.55:
                cut = para
            else:
                sentences = [m.end() for m in SENTENCE_END.finditer(window)]
                for s in reversed(sentences):
                    if s > size * 0.55:
                        cut = s
                        break
            if cut > 0:
                end = pos + cut
        piece = text[pos:end]
        if piece.strip():
            out.append((piece, pos, end))
        if end >= n:
            break
        pos = max(end - overlap, pos + 1)
    return out


def build_chunks(
    chapter: ChapterSpan,
    parent_size: int,
    child_size: int,
    overlap: int,
) -> tuple[list[tuple[DraftChunk, list[DraftChunk]]], list[dict]]:
    """返回 [(父块, [子块...])] 及本章告警。"""
    issues: list[dict] = []
    pairs: list[tuple[DraftChunk, list[DraftChunk]]] = []
    base = chapter.char_start

    # 先在章内按场景分隔符切成"场景"，再在场景内建父块，
    # 避免一个父块横跨两个完全无关的场景。
    scenes: list[tuple[str, int]] = []
    cursor = 0
    for m in SCENE_SEP.finditer(chapter.text):
        if m.start() > cursor:
            scenes.append((chapter.text[cursor : m.start()], cursor))
        cursor = m.end()
    if cursor < len(chapter.text):
        scenes.append((chapter.text[cursor:], cursor))
    if not scenes:
        scenes = [(chapter.text, 0)]

    p_seq = 0
    c_seq = 0
    for scene_text, scene_off in scenes:
        if not scene_text.strip():
            continue
        for piece, p_start, p_end in _split_with_boundary(scene_text, 0, parent_size, 0):
            p_seq += 1
            parent = DraftChunk(
                level=0,
                chapter_no=chapter.chapter_no,
                seq=p_seq,
                title=chapter.title,
                content=piece.strip(),
                char_start=base + scene_off + p_start,
                char_end=base + scene_off + p_end,
                token_est=estimate_tokens(piece),
                parent_key=f"{chapter.chapter_no}:{p_seq}",
            )
            if parent.token_est > parent_size * 1.3:
                issues.append(
                    {
                        "code": "CHUNK_TOO_LONG",
                        "severity": "warn",
                        "detail": f"父块 token≈{parent.token_est} 超过阈值，可能含无标点长段",
                        "char_start": parent.char_start,
                        "chapter_no": chapter.chapter_no,
                    }
                )

            children: list[DraftChunk] = []
            for c_piece, c_start, c_end in _split_with_boundary(piece, 0, child_size, overlap):
                c_seq += 1
                child = DraftChunk(
                    level=1,
                    chapter_no=chapter.chapter_no,
                    seq=c_seq,
                    title=chapter.title,
                    content=c_piece.strip(),
                    char_start=parent.char_start + c_start,
                    char_end=parent.char_start + c_end,
                    token_est=estimate_tokens(c_piece),
                    parent_key=parent.parent_key,
                )
                if child.token_est < 20:
                    issues.append(
                        {
                            "code": "CHUNK_TOO_SHORT",
                            "severity": "info",
                            "detail": f"子块仅 {child.token_est} token：{child.content[:30]}",
                            "char_start": child.char_start,
                            "chapter_no": chapter.chapter_no,
                        }
                    )
                children.append(child)
            if not children:
                issues.append(
                    {
                        "code": "PARENT_MISSING",
                        "severity": "error",
                        "detail": "父块未切出任何子块",
                        "char_start": parent.char_start,
                        "chapter_no": chapter.chapter_no,
                    }
                )
            pairs.append((parent, children))

    return pairs, issues
