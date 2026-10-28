"""图谱接口。P1-M5。

设计上把"读"和"建"分开：
- 读接口（overview / entities / subgraph / relations）全部同步直查 PG，毫秒级；
- 建接口只负责**入队**并立刻返回 job_id，抽取本身是几十分钟级的后台任务。

这样前端拿到 job_id 后直接用现有的任务轮询组件看进度，不需要为图谱单独写一套进度机制。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db import get_session
from app.models import Entity, Job, Novel, Relation
from app.services.graph import store
from app.workers import runner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["graph"])


async def _require_novel(db: AsyncSession, novel_id: str) -> Novel:
    novel = await db.get(Novel, novel_id)
    if novel is None:
        raise HTTPException(404, "小说不存在")
    return novel


@router.get("/graph/overview")
async def graph_overview(novel_id: str = Query(...), db: AsyncSession = Depends(get_session)) -> dict:
    await _require_novel(db, novel_id)
    return await store.overview(db, novel_id)


@router.get("/graph/entities")
async def graph_entities(
    novel_id: str = Query(...),
    q: str = "",
    type: str = "",
    page: int = 1,
    page_size: int = 30,
    order: str = "importance",
    db: AsyncSession = Depends(get_session),
) -> dict:
    await _require_novel(db, novel_id)
    return await store.list_entities(
        db, novel_id, q=q.strip(), etype=type, page=max(1, page), page_size=min(100, max(1, page_size)), order=order
    )


@router.get("/graph/entities/{entity_id}")
async def graph_entity_detail(
    entity_id: str, novel_id: str = Query(...), db: AsyncSession = Depends(get_session)
) -> dict:
    detail = await store.get_entity(db, novel_id, entity_id)
    if detail is None:
        raise HTTPException(404, "实体不存在")
    return detail


@router.get("/graph/subgraph")
async def graph_subgraph(
    novel_id: str = Query(...),
    center: str | None = None,
    hops: int = 2,
    include_pending: bool = False,
    max_nodes: int | None = None,
    db: AsyncSession = Depends(get_session),
) -> dict:
    await _require_novel(db, novel_id)
    return await store.subgraph(
        db, novel_id, center=center, hops=hops, include_pending=include_pending, max_nodes=max_nodes
    )


@router.get("/graph/relations")
async def graph_relations(
    novel_id: str = Query(...),
    status: str = "",
    relation: str = "",
    page: int = 1,
    page_size: int = 30,
    db: AsyncSession = Depends(get_session),
) -> dict:
    await _require_novel(db, novel_id)
    return await store.list_relations(
        db, novel_id, status=status, relation=relation, page=max(1, page), page_size=min(100, max(1, page_size))
    )


@router.get("/graph/status")
async def graph_status(novel_id: str = Query(...), db: AsyncSession = Depends(get_session)) -> dict:
    """当前图谱状态：已建成的规模 + 是否有正在跑的构建任务。"""
    novel = await _require_novel(db, novel_id)
    ent = (await db.execute(select(Entity.id).where(Entity.novel_id == novel_id).limit(1))).scalar()
    rel_n = (await db.execute(select(Relation.id).where(Relation.novel_id == novel_id).limit(1))).scalar()
    job = (
        await db.execute(
            select(Job).where(Job.novel_id == novel_id, Job.kind == "graph").order_by(Job.created_at.desc()).limit(1)
        )
    ).scalars().first()

    running = job is not None and job.status in ("PENDING", "RUNNING")
    if running and job is not None:
        await db.refresh(job)  # worker 在别的会话里改状态，这里取最新
    return {
        "has_graph": bool(ent) or bool(rel_n),
        "built": (novel.meta or {}).get("graph"),
        "job": None
        if job is None
        else {
            "id": job.id,
            "status": job.status,
            "stage": job.stage,
            "progress": job.progress,
            "chapters_done": job.chapters_done,
            "chapters_total": job.chapters_total,
            "error": job.error,
            "stats": job.stats,
            "running": job.status in ("PENDING", "RUNNING"),
        },
    }


@router.post("/graph/build")
async def graph_build(
    novel_id: str = Query(...),
    force: bool = False,
    db: AsyncSession = Depends(get_session),
) -> dict:
    """入队一次图谱构建。已有任务在跑时直接返回它，不重复起。"""
    if not settings.graph_enabled:
        raise HTTPException(400, "图谱功能已关闭（GRAPH_ENABLED=false）")
    novel = await _require_novel(db, novel_id)
    if novel.status not in ("READY", "GRAPH_READY"):
        raise HTTPException(400, f"小说状态为 {novel.status}，需要先完成入库（READY）")

    existing = (
        await db.execute(
            select(Job)
            .where(Job.novel_id == novel_id, Job.kind == "graph", Job.status.in_(("PENDING", "RUNNING")))
            .order_by(Job.created_at.desc())
            .limit(1)
        )
    ).scalars().first()
    if existing is not None:
        return {"job_id": existing.id, "reused": True, "message": "已有构建任务在执行"}

    if not force:
        has = (await db.execute(select(Entity.id).where(Entity.novel_id == novel_id).limit(1))).scalar()
        if has:
            return {
                "job_id": None,
                "reused": True,
                "message": "图谱已存在，如需重建请带 force=true（会清空旧图重新抽取）",
            }

    job = Job(novel_id=novel_id, kind="graph", status="PENDING", stage="PENDING")
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await runner.submit(job.id, "graph")
    logger.info("graph build queued novel=%s job=%s", novel_id, job.id)
    return {"job_id": job.id, "reused": False, "message": "已入队，正在抽取"}
