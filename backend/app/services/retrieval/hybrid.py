"""检索：稠密召回 → 父块回溯。

P0 只有稠密通道（bge-small 不出稀疏）。P1 加 BM25 + RRF + reranker，
接口保持不变：`retrieve()` 返回的仍是带引用的父块列表。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Chapter, Chunk
from app.services.embedding.factory import get_embedder
from app.services.vector.factory import get_vector_store

logger = logging.getLogger(__name__)


@dataclass
class Citation:
    chunk_id: str
    parent_id: str
    chapter_no: int
    title: str
    char_start: int
    char_end: int
    score: float
    snippet: str
    content: str = ""

    def to_dict(self, with_content: bool = False) -> dict:
        d = {
            "chunk_id": self.chunk_id,
            "chapter_no": self.chapter_no,
            "title": self.title,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "score": round(self.score, 4),
            "snippet": self.snippet,
        }
        if with_content:
            d["content"] = self.content
        return d


@dataclass
class RetrievalResult:
    query: str
    citations: list[Citation] = field(default_factory=list)
    degraded: bool = False
    detail: dict = field(default_factory=dict)


async def retrieve(novel_id: str, question: str, top_k: int | None = None) -> RetrievalResult:
    top_k = top_k or settings.retrieve_top_k
    embedder = get_embedder()
    store = await get_vector_store()

    vector = await embedder.embed_query(question)
    hits = await store.search(novel_id, vector, top_k=top_k, level=1)
    if not hits:
        return RetrievalResult(query=question, detail={"hits": 0})

    # 同一父块的多个子块只保留最高分那个，避免"重复引用同一段"
    best_by_parent: dict[str, object] = {}
    for h in hits:
        key = h.parent_id or h.id
        if key not in best_by_parent or h.score > best_by_parent[key].score:  # type: ignore[attr-defined]
            best_by_parent[key] = h

    ordered = sorted(best_by_parent.values(), key=lambda x: x.score, reverse=True)  # type: ignore[attr-defined]
    ordered = ordered[: settings.context_parents]

    parent_ids = [h.parent_id for h in ordered if h.parent_id]
    contents: dict[str, str] = {}
    chapter_titles: dict[int, str] = {}
    if parent_ids:
        async with SessionLocal() as db:
            rows = (await db.execute(select(Chunk).where(Chunk.id.in_(parent_ids)))).scalars().all()
            contents = {r.id: r.content for r in rows}
            chs = (await db.execute(select(Chapter).where(Chapter.novel_id == novel_id))).scalars().all()
            chapter_titles = {c.chapter_no: c.title for c in chs}

    citations: list[Citation] = []
    for h in ordered:
        body = contents.get(h.parent_id) or h.content
        citations.append(
            Citation(
                chunk_id=h.id,
                parent_id=h.parent_id,
                chapter_no=h.chapter_no,
                title=h.title or chapter_titles.get(h.chapter_no, f"第{h.chapter_no}章"),
                char_start=h.char_start,
                char_end=h.char_end,
                score=h.score,
                snippet=h.content[:120].replace("\n", " "),
                content=body,
            )
        )

    return RetrievalResult(
        query=question,
        citations=citations,
        degraded=getattr(store, "name", "") != "milvus" or getattr(embedder, "degraded", False),
        detail={"hits": len(hits), "parents": len(citations)},
    )


def build_context(citations: list[Citation], max_chars: int = 8000) -> str:
    """上下文预算：单次不超过 8000 字，从高分往低分装，装不下就截断最后一段。"""
    parts: list[str] = []
    used = 0
    for i, c in enumerate(citations, start=1):
        block = f"[资料{i} · {c.title} · 位置{c.char_start}-{c.char_end}]\n{c.content}"
        if used + len(block) > max_chars:
            remain = max_chars - used
            if remain > 300:
                parts.append(block[:remain] + "…")
            break
        parts.append(block)
        used += len(block)
    return "\n\n".join(parts)
