"""混合检索：稠密 + 词法 → RRF 融合 → cross-encoder 精排 → 父块回溯。

P0 只有稠密通道，问题很具体：
- 专有名词（人名、门派、法宝）在 1024 维语义空间里经常撞车，"陆沉" 和 "苏晚" 的向量
  距离可能比 "陆沉" 和 "那个守夜人" 更远；
- 读者提问里出现的确切字串，恰恰是最可靠的信号，稠密通道反而把它稀释掉了。

所以 P1 加一条词法通道，两路各取 50，用 RRF 融合（不看分值只看排名，换模型不用重调参），
再用 cross-encoder 精排 top 50 → top 24，最后按父块去重压到 8 个送进 prompt。

**降级链**（P1-2 验收要求三种模式都有日志与标记）：
    hybrid（两路都有）→ dense_only（词法挂了/查询无法分词）→ sparse_only（向量库挂了）
    → empty（都挂，返回空结果由上层兜底）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Chapter, Chunk
from app.services.embedding.factory import get_embedder
from app.services.rerank.factory import get_reranker
from app.services.retrieval import lexical
from app.services.retrieval.fusion import rrf
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
    # 这一块是被哪几路召回的、在各路排第几（前端可展示"命中来源"，排查也靠它）
    channels: dict = field(default_factory=dict)
    rerank_score: float | None = None
    rrf_score: float = 0.0

    def to_dict(self, with_content: bool = False) -> dict:
        d = {
            "chunk_id": self.chunk_id,
            "chapter_no": self.chapter_no,
            "title": self.title,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "score": round(self.score, 4),
            "snippet": self.snippet,
            "channels": self.channels,
            "rerank_score": round(self.rerank_score, 4) if self.rerank_score is not None else None,
            "rrf_score": round(self.rrf_score, 5),
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
    mode: str = "hybrid"

    def to_dict(self, with_content: bool = False) -> dict:
        return {
            "query": self.query,
            "mode": self.mode,
            "degraded": self.degraded,
            "detail": self.detail,
            "items": [c.to_dict(with_content) for c in self.citations],
        }


@dataclass
class _Cand:
    """融合后的候选（还没回溯父块）。"""

    chunk_id: str
    parent_id: str
    chapter_no: int
    title: str
    char_start: int
    char_end: int
    content: str
    rrf_score: float
    channels: dict
    raw: dict
    rerank_score: float | None = None

    @property
    def display_score(self) -> float:
        if self.rerank_score is not None:
            return self.rerank_score
        # 没有精排时优先用稠密分（0-1 可读），其次词法分
        return float(self.raw.get("dense", self.raw.get("lexical", self.rrf_score)))


async def _dense_recall(novel_id: str, question: str) -> tuple[list[tuple[str, float]], str | None]:
    try:
        embedder = get_embedder()
        store = await get_vector_store()
        vector = await embedder.embed_query(question)
        hits = await store.search(
            novel_id, vector, top_k=settings.retrieve_dense_top_k, level=1
        )
        return [(h.id, h.score) for h in hits], None
    except Exception as exc:  # noqa: BLE001
        logger.warning("稠密召回失败 novel=%s: %s", novel_id, exc, exc_info=True)
        return [], f"dense:{type(exc).__name__}"


async def _lexical_recall(novel_id: str, question: str) -> tuple[list[tuple[str, float]], str | None]:
    if not settings.lexical_enabled:
        return [], None
    try:
        return await lexical.search(novel_id, question, limit=settings.retrieve_sparse_top_k), None
    except Exception as exc:  # noqa: BLE001
        logger.warning("词法召回失败 novel=%s: %s", novel_id, exc, exc_info=True)
        return [], f"lexical:{type(exc).__name__}"


async def retrieve(
    novel_id: str,
    question: str,
    top_k: int | None = None,
    *,
    extra_queries: list[str] | None = None,
) -> RetrievalResult:
    """多路召回。

    `extra_queries` 是**上下文增强的 query**（多轮追问时，用历史实体补全后的问句）。
    每路 query 各自跑稠密+词法，命名成 `dense@0` / `lexical@0` / `dense@1` …，
    再统一 RRF 融合——这样"原问句"和"增强问句"是平权的，
    增强问句召回错东西时不会把正确答案挤掉（它只多一路投票，不独占）。
    """
    final_n = min(top_k or settings.context_parents, settings.context_parents)

    queries = [question] + [q for q in (extra_queries or []) if q and q != question]
    per_query: list[tuple[str, list, str | None, list, str | None]] = []
    for q in queries:
        dense, err_d = await _dense_recall(novel_id, q)
        sparse, err_s = await _lexical_recall(novel_id, q)
        per_query.append((q, dense, err_d, sparse, err_s))

    channels: dict[str, list[tuple[str, float]]] = {}
    for i, (_q, dense, _ed, sparse, _es) in enumerate(per_query):
        if dense:
            channels[f"dense@{i}"] = dense
        if sparse:
            channels[f"lexical@{i}"] = sparse

    err_d = next((e for _q, _d, e, _s, _es in per_query if e), None)
    err_s = next((e for _q, _d, _ed, _s, e in per_query if e), None)

    if not channels:
        mode = "empty"
        logger.warning(
            "检索双通道均无结果 novel=%s q=%r errors=%s", novel_id, question[:30], [err_d, err_s]
        )
        return RetrievalResult(
            query=question,
            mode=mode,
            degraded=True,
            detail={"dense": 0, "lexical": 0, "fused": 0, "errors": [e for e in (err_d, err_s) if e]},
        )

    # 模式判定用**第一路（原问句）**的召回情况，而不是最后一路。
    # 多路循环里 dense/sparse 会被逐路覆盖，直接用它们会拿到
    # "最后一路 query 命中了没有"，与本次问句无关。
    _q0, dense0, _ed0, sparse0, _es0 = per_query[0]
    if dense0 and sparse0:
        mode = "hybrid"
    elif dense0:
        mode = "dense_only"
        logger.info("检索降级 dense_only novel=%s reason=%s", novel_id, err_s or "词法无命中")
    else:
        mode = "sparse_only"
        logger.warning("检索降级 sparse_only novel=%s reason=%s", novel_id, err_d or "稠密无命中")

    fused = rrf(channels, k=settings.rrf_k)
    head = fused[: settings.rerank_input_k]

    # ---- 回表取正文与元数据（词法通道只返回 id，统一在这里补全）----
    ids = [f.chunk_id for f in head]
    async with SessionLocal() as db:
        rows = (await db.execute(select(Chunk).where(Chunk.id.in_(ids)))).scalars().all()
    by_id = {r.id: r for r in rows}

    cands: list[_Cand] = []
    for f in head:
        r = by_id.get(f.chunk_id)
        if r is None:  # 向量库里有、关系库里没有（脏数据），跳过而不是报错
            continue
        cands.append(
            _Cand(
                chunk_id=r.id,
                parent_id=r.parent_id or "",
                chapter_no=r.chapter_no,
                title=r.title,
                char_start=r.char_start,
                char_end=r.char_end,
                content=r.content,
                rrf_score=f.score,
                channels=f.ranks,
                raw=f.raw,
            )
        )
    if not cands:
        return RetrievalResult(
            query=question,
            mode=mode,
            degraded=True,
            detail={"dense": len(dense), "lexical": len(sparse), "fused": len(fused), "hydrated": 0},
        )

    # ---- 精排 ----
    reranker = get_reranker()
    reranked = False
    rerank_error = ""
    if settings.rerank_enabled and reranker.ready and len(cands) > 1:
        try:
            scores = await reranker.rerank(question, [c.content for c in cands], top_n=settings.rerank_top_n)
            for c, s in zip(cands, scores, strict=False):
                c.rerank_score = float(s)
            cands.sort(key=lambda c: c.rerank_score if c.rerank_score is not None else 0.0, reverse=True)
            reranked = True
        except Exception as exc:  # noqa: BLE001
            rerank_error = f"{type(exc).__name__}: {exc}"
            logger.warning("精排失败，退回 RRF 排序: %s", rerank_error)
    elif settings.rerank_enabled and not reranker.ready:
        rerank_error = reranker.error or "模型未就绪"

    # ---- 父块去重：同一 parent 只留排最前的那块，避免重复引用同一段 ----
    seen: dict[str, _Cand] = {}
    for c in cands:
        key = c.parent_id or c.chunk_id
        if key not in seen:
            seen[key] = c
    ordered = list(seen.values())[:final_n]

    parent_ids = [c.parent_id for c in ordered if c.parent_id]
    contents: dict[str, str] = {}
    chapter_titles: dict[int, str] = {}
    async with SessionLocal() as db:
        if parent_ids:
            prows = (await db.execute(select(Chunk).where(Chunk.id.in_(parent_ids)))).scalars().all()
            contents = {r.id: r.content for r in prows}
        chs = (await db.execute(select(Chapter).where(Chapter.novel_id == novel_id))).scalars().all()
        chapter_titles = {c.chapter_no: c.title for c in chs}

    citations = [
        Citation(
            chunk_id=c.chunk_id,
            parent_id=c.parent_id,
            chapter_no=c.chapter_no,
            title=c.title or chapter_titles.get(c.chapter_no, f"第{c.chapter_no}章"),
            char_start=c.char_start,
            char_end=c.char_end,
            score=c.display_score,
            snippet=c.content[:120].replace("\n", " "),
            content=contents.get(c.parent_id) or c.content,
            channels=c.channels,
            rerank_score=c.rerank_score,
            rrf_score=c.rrf_score,
        )
        for c in ordered
    ]

    detail = {
        "dense": len(dense0),
        "lexical": len(sparse0),
        "fused": len(fused),
        "candidates": len(cands),
        "parents": len(citations),
        "reranked": reranked,
        "rerank_model": settings.rerank_model if reranked else "",
    }
    # 多路召回时把每一路单独记一笔：排查"追问召回了什么"时，
    # 能分清是原问句召的、还是增强问句召的。
    if len(queries) > 1:
        detail["queries"] = [
            {
                "query": q,
                "dense": len(d),
                "lexical": len(s),
            }
            for q, d, _ed, s, _es in per_query
        ]
    if rerank_error:
        detail["rerank_error"] = rerank_error
    if err_d or err_s:
        detail["errors"] = [e for e in (err_d, err_s) if e]

    return RetrievalResult(
        query=question,
        citations=citations,
        mode=mode,
        degraded=mode != "hybrid",
        detail=detail,
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
