"""任务执行器。

P0 不引入 Celery/Redis：单机单进程 + 队列串行执行，够用且零运维。
CPU 只有 4 核，并发跑两个入库任务只会互相抢线程、双双变慢，
所以队列**故意串行**。切 Celery 时只需换 `submit()` 的实现。
"""

from __future__ import annotations

import asyncio
import logging

from app.services.ingestion.pipeline import rebuild_vectors, run_ingest
from app.workers.cancel import IngestCancelled, clear

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
            if kind in ("rebuild", "rebuild_force"):
                job = await _job_novel(job_id)
                await rebuild_vectors(job, job_id, resume=(kind == "rebuild"))
            else:
                await run_ingest(job_id)
        except IngestCancelled as exc:
            # 取消发生在 delete_novel 之后：期间可能又写进去一批向量，收尾时再清一次
            logger.warning("job %s 已取消（小说 %s 被删除）", job_id, exc.novel_id)
            try:
                from app.services.vector.factory import get_vector_store

                store = await get_vector_store()
                await store.delete_novel(exc.novel_id)
            except Exception:  # noqa: BLE001
                logger.warning("取消后清理残留向量失败 novel=%s", exc.novel_id, exc_info=True)
            clear(exc.novel_id)
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


async def recover_stale() -> int:
    """启动时清理上一次进程退出时残留的任务。

    内存队列不持久化，重启后没人会去执行这些任务，但 DB 里状态仍是 RUNNING，
    UI 上就永远显示"还在跑 84%"——看起来像卡死，实际是幽灵任务。
    必须在 worker 启动前把状态翻成 FAILED，让用户能明确重试。
    """
    from datetime import datetime

    from sqlalchemy import select

    from app.db import SessionLocal
    from app.models import Job, Novel

    async with SessionLocal() as db:
        rows = (
            await db.execute(select(Job).where(Job.status.in_(("RUNNING", "PENDING"))))
        ).scalars().all()
        if not rows:
            return 0
        novel_ids = set()
        requeue: list[str] = []
        for job in rows:
            # 只是"排着队没轮到"的任务不算中断：内存队列已随进程消失，重新入队即可，
            # 否则用户刚上传的书会被莫名标成 FAILED，还得手动再点一次入库。
            if job.status == "PENDING" and job.started_at is None and not job.progress:
                requeue.append(job.id)
                continue
            job.status = "FAILED"
            job.error = (
                f"服务重启导致中断（中断时 {job.stage} {job.progress:.1f}%，"
                f"已写 {job.vectors_written} 条向量），请重新入库"
            )
            job.finished_at = datetime.utcnow()
            novel_ids.add(job.novel_id)
        novels = (
            await db.execute(select(Novel).where(Novel.id.in_(novel_ids)))
        ).scalars().all()
        for n in novels:
            if n.status in ("PENDING", "VECTORIZING", "RUNNING"):
                n.status = "FAILED"
        await db.commit()
        if novel_ids:
            logger.warning(
                "清理 %d 个残留任务（服务重启中断）：%s", len(novel_ids), sorted(novel_ids)
            )
        for job_id in requeue:
            await submit(job_id, "ingest")
        if requeue:
            logger.info("重新入队 %d 个未开始的任务：%s", len(requeue), requeue)
        return len(novel_ids)


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
