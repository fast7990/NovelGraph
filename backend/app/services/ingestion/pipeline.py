"""入库流水线：解析 → 分章 → 切块 → 向量化 → 写库。

三条不可妥协的约束
1. **字符进度**：进度按"已处理字符 / 总字符"，块数事前未知，用它会出现假进度。
2. **可重跑**：同一个 novel 重复跑不会产生重复向量（先按 novel_id 删再写）。
3. **阶段隔离**：向量写失败不回滚切片结果 —— 切一次很贵，向量可以单独重算。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import delete, func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Chapter, Chunk, ChunkReport, Job, Novel
from app.services.embedding.factory import get_embedder
from app.services.ingestion import chunker, parser
from app.services.storage import get_storage
from app.services.vector.factory import get_vector_store
from app.workers.cancel import IngestCancelled, is_cancelled

logger = logging.getLogger(__name__)

EMBED_ROWS_PER_BATCH = 128
TOKEN_BUCKETS = [0, 100, 200, 300, 400, 512, 800, 1200, 2000]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Progress:
    """节流写入，避免每处理一个块就 UPDATE 一次数据库。"""

    job_id: str
    last: float = 0.0
    interval: float = 0.4
    pending: dict = field(default_factory=dict)

    async def update(self, force: bool = False, **fields) -> None:
        self.pending.update(fields)
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            return
        self.last = now
        payload = dict(self.pending)
        self.pending.clear()
        async with SessionLocal() as db:
            job = await db.get(Job, self.job_id)
            if job is None:
                return
            for k, v in payload.items():
                setattr(job, k, v)
            await db.commit()


async def run_ingest(job_id: str) -> None:
    started = time.monotonic()
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            logger.error("job %s 不存在", job_id)
            return
        novel = await db.get(Novel, job.novel_id)
        if novel is None:
            job.status = "FAILED"
            job.error = "小说记录不存在"
            await db.commit()
            return
        novel_id = novel.id
        storage_key = novel.storage_key
        job.status = "RUNNING"
        job.stage = "PARSING"
        job.started_at = _utcnow()
        novel.status = "PROCESSING"
        await db.commit()

    progress = Progress(job_id)
    try:
        # ---------- 1. 解析 ----------
        storage = get_storage()
        raw = await storage.get(storage_key)
        result = await asyncio.to_thread(parser.parse, raw)

        # 规范化文本回写：之后所有偏移都以它为准
        await storage.put(storage_key, result.text.encode("utf-8"))
        await progress.update(
            force=True,
            stage="CHUNKING",
            progress=5.0,
            chars_total=result.char_count,
            chapters_total=len(result.chapters),
            stats={"encoding": result.encoding, "chapters": len(result.chapters)},
        )

        async with SessionLocal() as db:
            novel = await db.get(Novel, novel_id)
            novel.encoding = result.encoding
            novel.char_count = result.char_count
            novel.chapter_count = len(result.chapters)
            novel.title = novel.title or (result.chapters[0].title if result.chapters else novel.filename)
            # 重跑：清掉旧切片，保证幂等
            await db.execute(delete(Chunk).where(Chunk.novel_id == novel_id))
            await db.execute(delete(ChunkReport).where(ChunkReport.novel_id == novel_id))
            await db.execute(delete(Chapter).where(Chapter.novel_id == novel_id))
            for span in result.chapters:
                db.add(
                    Chapter(
                        novel_id=novel_id,
                        chapter_no=span.chapter_no,
                        title=span.title[:200],
                        char_start=span.char_start,
                        char_end=span.char_end,
                        char_count=len(span.text),
                    )
                )
            for issue in result.issues:
                db.add(
                    ChunkReport(
                        novel_id=novel_id,
                        job_id=job_id,
                        chapter_no=int(issue.get("chapter_no", 0)),
                        code=issue["code"],
                        severity=issue.get("severity", "warn"),
                        detail=issue.get("detail", "")[:500],
                        char_start=int(issue.get("char_start", 0)),
                    )
                )
            await db.commit()

        # ---------- 2. 切块 ----------
        embed_pool: list[dict] = []   # 待向量化的 child
        issues: list[dict] = list(result.issues)
        chars_done = 0
        chunk_count = 0
        token_hist = [0] * (len(TOKEN_BUCKETS) - 1)

        for idx, span in enumerate(result.chapters, start=1):
            pairs, ch_issues = chunker.build_chunks(
                span,
                parent_size=settings.chunk_parent_size,
                child_size=settings.chunk_child_size,
                overlap=settings.chunk_overlap,
            )
            issues.extend(ch_issues)
            async with SessionLocal() as db:
                for parent, children in pairs:
                    p_row = Chunk(
                        novel_id=novel_id,
                        level=0,
                        chapter_no=parent.chapter_no,
                        seq=parent.seq,
                        title=parent.title[:200],
                        content=parent.content,
                        char_start=parent.char_start,
                        char_end=parent.char_end,
                        token_est=parent.token_est,
                    )
                    db.add(p_row)
                    await db.flush()  # 拿 parent_id
                    for child in children:
                        db.add(
                            Chunk(
                                novel_id=novel_id,
                                parent_id=p_row.id,
                                level=1,
                                chapter_no=child.chapter_no,
                                seq=child.seq,
                                title=child.title[:200],
                                content=child.content,
                                char_start=child.char_start,
                                char_end=child.char_end,
                                token_est=child.token_est,
                            )
                        )
                        chunk_count += 1
                        bucket = 0
                        for i in range(len(TOKEN_BUCKETS) - 1):
                            if child.token_est >= TOKEN_BUCKETS[i]:
                                bucket = i
                        token_hist[bucket] += 1
                for issue in ch_issues:
                    db.add(
                        ChunkReport(
                            novel_id=novel_id,
                            job_id=job_id,
                            chapter_no=int(issue.get("chapter_no", 0)),
                            code=issue["code"],
                            severity=issue.get("severity", "warn"),
                            detail=issue.get("detail", "")[:500],
                            char_start=int(issue.get("char_start", 0)),
                        )
                    )
                await db.commit()

            chars_done += len(span.text)
            await progress.update(
                chars_done=min(chars_done, result.char_count),
                chapters_done=idx,
                chunks_created=chunk_count,
                progress=5.0 + 30.0 * (chars_done / max(result.char_count, 1)),
            )

        # 切片完成后统一取回 child（含 parent_id），避免在循环里反复查库
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(Chunk).where(Chunk.novel_id == novel_id, Chunk.level == 1).order_by(Chunk.chapter_no, Chunk.seq)
                )
            ).scalars().all()
            embed_pool = [
                {
                    "id": r.id,
                    "novel_id": novel_id,
                    "level": 1,
                    "chapter_no": r.chapter_no,
                    "seq": r.seq,
                    "parent_id": r.parent_id or "",
                    "title": r.title,
                    "content": r.content,
                    "char_start": r.char_start,
                    "char_end": r.char_end,
                }
                for r in rows
            ]
            novel = await db.get(Novel, novel_id)
            novel.chunk_count = chunk_count
            novel.status = "VECTORIZING"
            await db.commit()

        await progress.update(force=True, stage="EMBEDDING", progress=35.0)

        # ---------- 3. 向量化 + 写入 ----------
        embedder = get_embedder()
        store = await get_vector_store()
        await store.ensure_ready(embedder.dim)
        await store.delete_novel(novel_id)   # 幂等：先清后写

        total = len(embed_pool)
        written = 0
        for i in range(0, total, EMBED_ROWS_PER_BATCH):
            if is_cancelled(novel_id):          # 书被删了就别再烧 CPU
                raise IngestCancelled(novel_id)
            batch = embed_pool[i : i + EMBED_ROWS_PER_BATCH]
            vectors = await embedder.embed_documents([b["content"] for b in batch])
            rows = []
            for meta, vec in zip(batch, vectors, strict=True):
                rows.append({**meta, "dense": vec})
            written += await store.upsert(rows)
            await progress.update(
                vectors_written=written,
                progress=35.0 + 60.0 * (min(i + EMBED_ROWS_PER_BATCH, total) / max(total, 1)),
            )

        # ---------- 4. 收尾校验 ----------
        await progress.update(force=True, stage="INDEXING", progress=97.0)
        stored = await store.count(novel_id)
        duration = round(time.monotonic() - started, 1)

        issue_summary: dict[str, int] = {}
        for issue in issues:
            issue_summary[issue["code"]] = issue_summary.get(issue["code"], 0) + 1

        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            job.status = "COMPLETED"
            job.stage = "COMPLETED"
            job.progress = 100.0
            job.chars_done = result.char_count
            job.vectors_written = written
            job.chunks_created = chunk_count
            job.chapters_done = len(result.chapters)
            job.finished_at = _utcnow()
            job.stats = {
                "encoding": result.encoding,
                "char_count": result.char_count,
                "chapters": len(result.chapters),
                "chunks": chunk_count,
                "vectors_written": written,
                "vectors_stored": stored,
                "embedder": getattr(embedder, "name", "?"),
                "embedder_dim": embedder.dim,
                "vector_backend": getattr(store, "name", "?"),
                "duration_sec": duration,
                "chars_per_sec": round(result.char_count / max(duration, 0.1)),
                "token_hist": token_hist,
                "token_buckets": TOKEN_BUCKETS,
                "issues_by_code": issue_summary,
                "issue_total": len(issues),
            }
            novel = await db.get(Novel, novel_id)
            novel.status = "READY"
            await db.commit()

        logger.info(
            "ingest done novel=%s chars=%s chunks=%s vectors=%s %.1fs",
            novel_id,
            result.char_count,
            chunk_count,
            written,
            duration,
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("ingest failed job=%s", job_id)
        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            if job:
                job.status = "FAILED"
                job.stage = "FAILED"
                job.error = f"{type(exc).__name__}: {exc}"[:2000]
                job.finished_at = _utcnow()
            novel = await db.get(Novel, novel_id)
            if novel:
                novel.status = "FAILED"
            await db.commit()


async def rebuild_vectors(novel_id: str, job_id: str, resume: bool = True) -> None:
    """只重算向量，不重新解析切片（切片很贵，向量便宜）。

    resume=True 时跳过库里已有的 chunk：一本书百万字级要跑一两个小时，
    中途重启或改配置后重来一次的成本太高。只有显式 force 才清空重写。
    """
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        job.status = "RUNNING"
        job.stage = "EMBEDDING"
        await db.commit()
        rows = (
            await db.execute(select(Chunk).where(Chunk.novel_id == novel_id, Chunk.level == 1).order_by(Chunk.chapter_no, Chunk.seq))
        ).scalars().all()
        total_chars = (await db.execute(select(func.coalesce(func.sum(Chunk.char_end - Chunk.char_start), 0)).where(Chunk.novel_id == novel_id))).scalar() or 1

    progress = Progress(job_id)
    embedder = get_embedder()
    store = await get_vector_store()
    await store.ensure_ready(embedder.dim)

    total_all = len(rows)
    skipped = 0
    if resume:
        done = await store.existing_ids(novel_id)
        if done:
            rows = [r for r in rows if r.id not in done]
            skipped = total_all - len(rows)
            logger.info("续跑 %s：已完成 %d/%d，本次处理 %d", novel_id, skipped, total_all, len(rows))
    else:
        await store.delete_novel(novel_id)

    if skipped:
        await progress.update(
            progress=95.0 * skipped / max(total_all, 1),
            stats={"resumed_skipped": skipped},
        )

    written = 0
    done_chars = 0
    for i in range(0, len(rows), EMBED_ROWS_PER_BATCH):
        if is_cancelled(novel_id):              # 书被删了就别再烧 CPU
            raise IngestCancelled(novel_id)
        batch = rows[i : i + EMBED_ROWS_PER_BATCH]
        vectors = await embedder.embed_documents([r.content for r in batch])
        payload = [
            {
                "id": r.id,
                "novel_id": novel_id,
                "level": 1,
                "chapter_no": r.chapter_no,
                "seq": r.seq,
                "parent_id": r.parent_id or "",
                "title": r.title,
                "content": r.content,
                "char_start": r.char_start,
                "char_end": r.char_end,
                "dense": v,
            }
            for r, v in zip(batch, vectors, strict=True)
        ]
        written += await store.upsert(payload)
        done_chars += sum(r.char_end - r.char_start for r in batch)
        await progress.update(
            chars_done=min(done_chars, int(total_chars)),
            chars_total=int(total_chars),
            vectors_written=written,
            progress=95.0 * (min(skipped + i + EMBED_ROWS_PER_BATCH, total_all) / max(total_all, 1)),
        )

    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        job.status = "COMPLETED"
        job.stage = "COMPLETED"
        job.progress = 100.0
        job.vectors_written = written + skipped
        job.chars_done = int(total_chars)
        job.chars_total = int(total_chars)
        job.finished_at = _utcnow()
        stats = dict(job.stats or {})
        stats["vectors_written"] = written
        stats["resumed_skipped"] = skipped
        job.stats = stats
        await db.commit()
