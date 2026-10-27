"""小说与切片相关接口。"""

from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.models import Chapter, Chunk, ChunkReport, Job, Novel
from app.schemas import (
    ChunkOut,
    ChunkPage,
    ChunkReportOut,
    NovelListOut,
    NovelOut,
    ReportItem,
    UploadResponse,
)
from app.services.storage import get_storage
from app.workers import runner

logger = logging.getLogger(__name__)
router = APIRouter(tags=["novels"])

MAX_UPLOAD = 200 * 1024 * 1024
ALLOWED_SUFFIX = {".txt", ".md", ".text"}


@router.post("/novels", response_model=UploadResponse)
async def upload_novel(
    file: UploadFile = File(...),
    title: str = Form(""),
    db: AsyncSession = Depends(get_session),
) -> UploadResponse:
    filename = file.filename or "unnamed.txt"
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_SUFFIX:
        raise HTTPException(400, f"仅支持纯文本：{', '.join(sorted(ALLOWED_SUFFIX))}")

    data = await file.read()
    if not data:
        raise HTTPException(400, "文件为空")
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "文件超过 200MB，请先拆分")

    novel = Novel(title=title.strip() or Path(filename).stem, filename=filename, file_size=len(data), status="PENDING")
    db.add(novel)
    await db.flush()

    storage_key = f"novels/{novel.id}/source.txt"
    await get_storage().put(storage_key, data)
    novel.storage_key = storage_key

    job = Job(novel_id=novel.id, status="PENDING", stage="PENDING")
    db.add(job)
    novel.meta = {"original_filename": filename}
    await db.commit()
    await db.refresh(novel)
    await db.refresh(job)

    await runner.submit(job.id, "ingest")
    return UploadResponse(
        novel_id=novel.id,
        job_id=job.id,
        filename=filename,
        file_size=len(data),
    )


@router.get("/novels", response_model=NovelListOut)
async def list_novels(db: AsyncSession = Depends(get_session)) -> NovelListOut:
    rows = (await db.execute(select(Novel).order_by(Novel.created_at.desc()))).scalars().all()
    return NovelListOut(
        items=[NovelOut(**{k: getattr(r, k) for k in NovelOut.model_fields}) for r in rows],
        total=len(rows),
    )


@router.get("/novels/{novel_id}", response_model=NovelOut)
async def get_novel(novel_id: str, db: AsyncSession = Depends(get_session)) -> NovelOut:
    novel = await db.get(Novel, novel_id)
    if not novel:
        raise HTTPException(404, "小说不存在")
    return NovelOut(**{k: getattr(novel, k) for k in NovelOut.model_fields})


@router.delete("/novels/{novel_id}")
async def delete_novel(novel_id: str, db: AsyncSession = Depends(get_session)) -> dict:
    novel = await db.get(Novel, novel_id)
    if not novel:
        raise HTTPException(404, "小说不存在")
    await db.execute(delete(Chunk).where(Chunk.novel_id == novel_id))
    await db.execute(delete(ChunkReport).where(ChunkReport.novel_id == novel_id))
    await db.execute(delete(Chapter).where(Chapter.novel_id == novel_id))
    await db.execute(delete(Job).where(Job.novel_id == novel_id))
    await db.delete(novel)
    await db.commit()

    try:
        from app.services.vector.factory import get_vector_store

        store = await get_vector_store()
        await store.delete_novel(novel_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("删除向量失败 %s: %s", novel_id, exc)
    return {"deleted": novel_id}


@router.get("/novels/{novel_id}/chapters")
async def list_chapters(novel_id: str, db: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        await db.execute(select(Chapter).where(Chapter.novel_id == novel_id).order_by(Chapter.chapter_no))
    ).scalars().all()
    return {
        "items": [
            {
                "chapter_no": c.chapter_no,
                "title": c.title,
                "char_start": c.char_start,
                "char_end": c.char_end,
                "char_count": c.char_count,
            }
            for c in rows
        ]
    }


@router.get("/novels/{novel_id}/chunks", response_model=ChunkPage)
async def list_chunks(
    novel_id: str,
    level: int | None = Query(None, ge=0, le=3),
    chapter_no: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=200),
    db: AsyncSession = Depends(get_session),
) -> ChunkPage:
    stmt = select(Chunk).where(Chunk.novel_id == novel_id)
    count_stmt = select(func.count()).select_from(Chunk).where(Chunk.novel_id == novel_id)
    if level is not None:
        stmt = stmt.where(Chunk.level == level)
        count_stmt = count_stmt.where(Chunk.level == level)
    if chapter_no is not None:
        stmt = stmt.where(Chunk.chapter_no == chapter_no)
        count_stmt = count_stmt.where(Chunk.chapter_no == chapter_no)

    total = (await db.execute(count_stmt)).scalar() or 0
    rows = (
        await db.execute(
            stmt.order_by(Chunk.chapter_no, Chunk.seq).offset((page - 1) * page_size).limit(page_size)
        )
    ).scalars().all()
    return ChunkPage(
        items=[ChunkOut(**{k: getattr(r, k) for k in ChunkOut.model_fields}) for r in rows],
        total=total,
        page=page,
        page_size=page_size,
    )


@router.get("/novels/{novel_id}/chunk-report", response_model=ChunkReportOut)
async def chunk_report(novel_id: str, db: AsyncSession = Depends(get_session)) -> ChunkReportOut:
    novel = await db.get(Novel, novel_id)
    if not novel:
        raise HTTPException(404, "小说不存在")

    parents = (
        await db.execute(
            select(Chunk.char_start, Chunk.char_end).where(Chunk.novel_id == novel_id, Chunk.level == 0)
        )
    ).all()
    children = (
        await db.execute(
            select(Chunk.id, Chunk.chapter_no, Chunk.seq, Chunk.token_est)
            .where(Chunk.novel_id == novel_id, Chunk.level == 1)
            .order_by(Chunk.chapter_no, Chunk.seq)
        )
    ).all()
    reports = (
        await db.execute(
            select(ChunkReport).where(ChunkReport.novel_id == novel_id).order_by(ChunkReport.chapter_no, ChunkReport.code)
        )
    ).scalars().all()

    covered = sum(max(0, e - s) for s, e in parents)
    total_chars = novel.char_count or covered or 1
    tokens = [c.token_est for c in children]
    avg_tokens = round(sum(tokens) / len(tokens), 1) if tokens else 0.0
    p95 = int(sorted(tokens)[int(len(tokens) * 0.95) - 1]) if tokens else 0

    buckets = [0, 100, 200, 300, 400, 512, 800, 1200, 2000]
    hist = [0] * (len(buckets) - 1)
    for t in tokens:
        idx = 0
        for i in range(len(buckets) - 1):
            if t >= buckets[i]:
                idx = i
        hist[idx] += 1

    by_code: dict[str, int] = {}
    by_sev: dict[str, int] = {}
    for r in reports:
        by_code[r.code] = by_code.get(r.code, 0) + 1
        by_sev[r.severity] = by_sev.get(r.severity, 0) + 1

    # 每 20 章抽 1 块做人工抽检
    sample_ids = [c.id for c in children if c.chapter_no % 20 == 1][:10]
    samples = []
    if sample_ids:
        rows = (await db.execute(select(Chunk).where(Chunk.id.in_(sample_ids)))).scalars().all()
        samples = [ChunkOut(**{k: getattr(r, k) for k in ChunkOut.model_fields}) for r in rows]

    return ChunkReportOut(
        novel_id=novel_id,
        chapter_count=novel.chapter_count,
        chunk_count=len(children),
        parent_count=len(parents),
        char_count=novel.char_count,
        covered_chars=covered,
        completeness=round(min(covered / total_chars, 1.0), 4),
        avg_tokens=avg_tokens,
        p95_tokens=p95,
        token_hist=hist,
        token_buckets=buckets,
        issues=[
            ReportItem(
                chapter_no=r.chapter_no,
                code=r.code,
                severity=r.severity,
                detail=r.detail,
                char_start=r.char_start,
            )
            for r in reports[:200]
        ],
        issues_by_code=by_code,
        issues_by_severity=by_sev,
        sample_chunks=samples,
    )


@router.post("/novels/{novel_id}/chunks/rebuild")
async def rebuild(novel_id: str, db: AsyncSession = Depends(get_session)) -> dict:
    novel = await db.get(Novel, novel_id)
    if not novel:
        raise HTTPException(404, "小说不存在")
    job = Job(novel_id=novel_id, status="PENDING", stage="PENDING")
    db.add(job)
    novel.status = "PROCESSING"
    await db.commit()
    await db.refresh(job)
    await runner.submit(job.id, "ingest")
    return {"job_id": job.id, "novel_id": novel_id, "message": "已重新入队（全量重跑）"}


@router.get("/novels/{novel_id}/excerpt", response_class=PlainTextResponse)
async def excerpt(
    novel_id: str,
    char_start: int = Query(0, ge=0),
    char_end: int = Query(2000, ge=1),
    db: AsyncSession = Depends(get_session),
) -> str:
    """按字符区间取原文，前端引用跳转用。"""
    novel = await db.get(Novel, novel_id)
    if not novel:
        raise HTTPException(404, "小说不存在")
    data = await get_storage().get(novel.storage_key)
    text = data.decode("utf-8", errors="replace")
    return text[max(0, char_start) : min(len(text), char_end)]


@router.get("/novels/{novel_id}/vectors/sample")
async def vector_sample(novel_id: str, limit: int = Query(500, ge=1, le=2000)) -> dict:
    """向量库可视化（P1 页面的数据源，P0 先通接口）。"""
    from app.services.vector.factory import get_vector_store

    store = await get_vector_store()
    hits = await store.sample(novel_id, limit=limit)
    return {
        "backend": getattr(store, "name", "?"),
        "count": len(hits),
        "items": [
            {
                "id": h.id,
                "chapter_no": h.chapter_no,
                "seq": h.seq,
                "level": h.level,
                "char_start": h.char_start,
                "char_end": h.char_end,
                "parent_id": h.parent_id,
            }
            for h in hits
        ],
    }
