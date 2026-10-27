"""健康检查与系统统计。中台首页要能看到"每个依赖是否活着"。"""

from __future__ import annotations

import asyncio
import logging
import platform
import time

from fastapi import APIRouter
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Chunk, Job, Novel
from app.services.embedding.factory import get_embedder
from app.services.llm.client import get_llm
from app.services.vector.factory import get_vector_store
from app.workers import runner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["system"])

_started = time.time()


@router.get("/health")
async def health() -> dict:
    db_ok = True
    try:
        async with SessionLocal() as db:
            await db.execute(select(func.count()).select_from(Novel))
    except Exception as exc:  # noqa: BLE001
        db_ok = False
        logger.warning("db health failed: %s", exc)

    async def safe(coro, name: str) -> dict:
        try:
            return await coro
        except Exception as exc:  # noqa: BLE001
            return {"name": name, "status": "down", "detail": str(exc)[:300]}

    store = await get_vector_store()
    emb = get_embedder()
    llm = get_llm()
    from app.services.rerank.factory import get_reranker
    from app.services.retrieval import lexical as lexical_index

    reranker = get_reranker()

    async def lexical_health() -> dict:
        s = await lexical_index.stats()
        return {
            "name": "lexical",
            "status": "ok" if s["ready"] else "degraded",
            "engine": s.get("engine", "postgres-tsvector"),
            "indexed": s["indexed"],
            "child_chunks": s["child_chunks"],
        }

    results = await asyncio.gather(
        safe(store.health(), "vector"),
        safe(emb.health(), "embedding"),
        safe(llm.health(), "llm"),
        safe(lexical_health(), "lexical"),
        safe(reranker.health(), "rerank"),
    )
    deps = [
        {
            "name": "database",
            "status": "ok" if db_ok else "down",
            "engine": settings.database_url.split("://")[0],
        },
        *results,
    ]
    overall = "ok"
    if any(d.get("status") == "down" for d in deps):
        overall = "degraded"
    if not db_ok:
        overall = "down"

    return {
        "status": overall,
        "uptime_sec": round(time.time() - _started, 1),
        "env": settings.app_env,
        "python": platform.python_version(),
        "queue": runner.queue_size(),
        "config": {
            "vector_backend": getattr(store, "name", "?"),
            "embedding": getattr(emb, "name", "?"),
            "embedding_dim": getattr(emb, "dim", None),
            "embedding_threads": settings.embedding_threads,
            "llm_model": settings.llm_model,
            "llm_base_url": settings.llm_base_url,
            "vector_table": settings.pgvector_table,
            "vector_index": settings.pgvector_index,
            "chunk_child_size": settings.chunk_child_size,
            "chunk_parent_size": settings.chunk_parent_size,
            "retrieval_mode": "hybrid" if settings.lexical_enabled else "dense",
            "rerank": reranker.ready,
        },
        "deps": deps,
    }


@router.get("/system/stats")
async def stats() -> dict:
    async with SessionLocal() as db:
        novel_count = (await db.execute(select(func.count()).select_from(Novel))).scalar() or 0
        chunk_count = (await db.execute(select(func.count()).select_from(Chunk))).scalar() or 0
        total_chars = (await db.execute(select(func.coalesce(func.sum(Novel.char_count), 0)))).scalar() or 0
        running = (
            await db.execute(select(func.count()).select_from(Job).where(Job.status == "RUNNING"))
        ).scalar() or 0
    store = await get_vector_store()
    try:
        vector_count = await store.count()
    except Exception:  # noqa: BLE001
        vector_count = -1
    from app.services.rerank.factory import get_reranker
    from app.services.retrieval import lexical as lexical_index

    try:
        lex = await lexical_index.stats()
    except Exception:  # noqa: BLE001
        lex = {"indexed": -1, "child_chunks": -1, "ready": False}
    return {
        "novels": novel_count,
        "chunks": chunk_count,
        "chars": int(total_chars),
        "vectors": vector_count,
        "running_jobs": running,
        "vector_backend": getattr(store, "name", "?"),
        "embedding_dim": settings.embedding_dim,
        "embedding_model": settings.embedding_model,
        "embedding_threads": settings.embedding_threads,
        "vector_table": settings.pgvector_table,
        "lexical_indexed": lex["indexed"],
        "lexical_ready": lex["ready"],
        "rerank_model": settings.rerank_model,
        "rerank_ready": get_reranker().ready,
    }
