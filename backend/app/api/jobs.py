"""任务查询与 SSE 进度推送。"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Job, Novel
from app.schemas import JobOut
from app.workers import runner
from app.workers.cancel import cancel_job as signal_job_cancel

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _to_out(job: Job) -> JobOut:
    return JobOut(**{k: getattr(job, k) for k in JobOut.model_fields})


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(
    novel_id: str | None = None,
    status: str = "",
    kind: str = "",
    limit: int = Query(50, ge=1, le=200),
) -> list[JobOut]:
    """任务列表。status/kind 支持多值（逗号分隔），任务中心按状态筛"失败 + 已取消"一次查完。"""
    async with SessionLocal() as db:
        stmt = select(Job)
        if novel_id:
            stmt = stmt.where(Job.novel_id == novel_id)
        if status:
            wanted = [s.strip().upper() for s in status.split(",") if s.strip()]
            if wanted:
                stmt = stmt.where(Job.status.in_(wanted))
        if kind:
            kinds = [k.strip() for k in kind.split(",") if k.strip()]
            if kinds:
                stmt = stmt.where(Job.kind.in_(kinds))
        rows = (await db.execute(stmt.order_by(Job.created_at.desc()).limit(limit))).scalars().all()
    return [_to_out(j) for j in rows]


@router.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    """重试失败/已取消的任务：复用同一行 job（保留历史），只重置进度字段再入队。

    复用而不是新建一行，是为了让"同一本书的这张任务卡"在界面上原地复活；
    想查历史可以按 novel_id 看全部任务。同一本书已有任务在跑时直接返回它，
    避免连点两次起两个重复任务抢同一份 CPU。
    """
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if job.status not in ("FAILED", "CANCELED"):
            raise HTTPException(409, f"当前状态 {job.status}，只有失败/已取消的任务可以重试")

        running = (
            await db.execute(
                select(Job)
                .where(Job.novel_id == job.novel_id, Job.id != job.id, Job.status.in_(("PENDING", "RUNNING")))
                .order_by(Job.created_at.desc())
                .limit(1)
            )
        ).scalars().first()
        if running is not None:
            return {"job_id": running.id, "reused": True, "message": "这本书已有任务在执行，请等它跑完"}

        job.status = "PENDING"
        job.stage = "PENDING"
        job.progress = 0.0
        job.chars_done = 0
        job.chapters_done = 0
        job.vectors_written = 0
        job.error = ""
        job.stats = {}
        job.started_at = None
        job.finished_at = None
        novel = await db.get(Novel, job.novel_id)
        if novel is not None and job.kind != "graph":
            novel.status = "PROCESSING"
        await db.commit()
        await db.refresh(job)
        novel_id, kind = job.novel_id, job.kind

    await runner.submit(job.id, "graph" if kind == "graph" else ("rebuild" if kind == "rebuild" else "ingest"))
    logger.info("job retried job=%s novel=%s kind=%s", job.id, novel_id, kind)
    return {"job_id": job.id, "reused": False, "message": "已重新入队"}


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """取消任务。

    排队的直接改状态（worker 取到时会跳过）；跑着的置取消信号，
    长循环每批检查一次，通常 1-3 秒内停手。**不清数据**——
    取消是"先别算了"，不是"删掉这本书"。
    """
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            raise HTTPException(404, "任务不存在")
        if job.status not in ("PENDING", "RUNNING"):
            raise HTTPException(409, f"当前状态 {job.status}，无法取消")

        queued = job.status == "PENDING"
        if queued:
            job.status = "CANCELED"
            job.stage = "CANCELED"
            job.error = "用户取消（未开始）"
            job.finished_at = datetime.now(UTC)
            novel = await db.get(Novel, job.novel_id)
            if novel is not None and job.kind != "graph" and novel.status == "PROCESSING":
                novel.status = "CANCELED"
        else:
            job.error = "用户取消"
        await db.commit()
        novel_id, current = job.novel_id, job.status

    if not queued:
        signal_job_cancel(job_id)
    logger.info("job cancel requested job=%s novel=%s queued=%s", job_id, novel_id, queued)
    return {
        "ok": True,
        "status": current,
        "message": "已取消（尚未开始）" if queued else "已发出取消信号，当前批次结束后停止",
    }


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str) -> JobOut:
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return _to_out(job)


@router.get("/jobs/{job_id}/events")
async def job_events(job_id: str, request: Request) -> StreamingResponse:
    """SSE：stage（阶段切换）/ progress（批次进度）/ chunk_progress（切片完成度）/ done / error。

    轮询 DB 而不是用内存队列：进程重启后前端刷新仍能看到进度，
    代价是 300ms 一次 SQLite 查询（本地库亚毫秒级，可忽略）。
    """

    async def gen():
        stage: str | None = None
        last_progress = -1.0
        last_chars = -1
        while True:
            if await request.is_disconnected():
                break
            async with SessionLocal() as db:
                job = await db.get(Job, job_id)
            if job is None:
                yield _sse("error", {"message": "任务不存在"})
                break

            if job.stage != stage:
                stage = job.stage
                yield _sse(
                    "stage",
                    {
                        "stage": job.stage,
                        "status": job.status,
                        "chapters_total": job.chapters_total,
                        "chars_total": job.chars_total,
                    },
                )

            if job.stage in ("CHUNKING", "EMBEDDING") and job.chars_done != last_chars:
                last_chars = job.chars_done
                yield _sse(
                    "chunk_progress",
                    {
                        "stage": job.stage,
                        "chars_done": job.chars_done,
                        "chars_total": job.chars_total,
                        "chapters_done": job.chapters_done,
                        "chapters_total": job.chapters_total,
                        "chunks_created": job.chunks_created,
                        "vectors_written": job.vectors_written,
                        "progress": round(job.progress, 2),
                    },
                )

            if abs(job.progress - last_progress) >= 0.5:
                last_progress = job.progress
                yield _sse(
                    "progress",
                    {
                        "stage": job.stage,
                        "progress": round(job.progress, 2),
                        "chunks_created": job.chunks_created,
                        "vectors_written": job.vectors_written,
                    },
                )

            if job.status in ("COMPLETED", "FAILED", "CANCELED"):
                yield _sse(
                    "done",
                    {
                        "status": job.status,
                        "error": job.error,
                        "stats": job.stats or {},
                    },
                )
                break

            await asyncio.sleep(0.3)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
