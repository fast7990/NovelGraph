"""任务执行器。

P0 不引入 Celery/Redis：单机单进程 + 队列串行执行，够用且零运维。
CPU 只有 4 核，并发跑两个入库任务只会互相抢线程、双双变慢，
所以队列**故意串行**。切 Celery 时只需换 `submit()` 的实现。
"""

from __future__ import annotations

import asyncio
import logging

from app.services.ingestion.pipeline import rebuild_vectors, run_ingest

logger = logging.getLogger(__name__)

_queue: asyncio.Queue[tuple[str, str]] | None = None
_worker: asyncio.Task | None = None


def _get_queue() -> asyncio.Queue[tuple[str, str]]:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


async def submit(job_id: str, kind: str = "ingest") -> None:
    await _get_queue().put((job_id, kind))


async def _loop() -> None:
    q = _get_queue()
    while True:
        job_id, kind = await q.get()
        try:
            if kind == "rebuild":
                job = await _job_novel(job_id)
                await rebuild_vectors(job, job_id)
            else:
                await run_ingest(job_id)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            logger.exception("job %s 执行异常", job_id)
        finally:
            q.task_done()


async def _job_novel(job_id: str) -> str:
    from app.db import SessionLocal
    from app.models import Job

    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        return job.novel_id if job else ""


def start() -> None:
    global _worker
    if _worker is None or _worker.done():
        _worker = asyncio.create_task(_loop(), name="ingest-worker")
        logger.info("ingest worker started")


async def stop() -> None:
    global _worker
    if _worker and not _worker.done():
        _worker.cancel()
        try:
            await _worker
        except asyncio.CancelledError:
            pass
    _worker = None


def queue_size() -> int:
    return _get_queue().qsize() if _queue else 0
