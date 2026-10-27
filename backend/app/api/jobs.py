"""任务查询与 SSE 进度推送。"""

from __future__ import annotations

import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.db import SessionLocal
from app.models import Job
from app.schemas import JobOut

logger = logging.getLogger(__name__)
router = APIRouter(tags=["jobs"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def _to_out(job: Job) -> JobOut:
    return JobOut(**{k: getattr(job, k) for k in JobOut.model_fields})


@router.get("/jobs", response_model=list[JobOut])
async def list_jobs(novel_id: str | None = None, limit: int = Query(20, ge=1, le=100)) -> list[JobOut]:
    async with SessionLocal() as db:
        stmt = select(Job).order_by(Job.created_at.desc()).limit(limit)
        if novel_id:
            stmt = select(Job).where(Job.novel_id == novel_id).order_by(Job.created_at.desc()).limit(limit)
        rows = (await db.execute(stmt)).scalars().all()
    return [_to_out(j) for j in rows]


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
