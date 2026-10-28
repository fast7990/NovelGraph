"""图谱构建编排：读块 → L1 候选 → L2 逐块抽取 → L3 对齐 → 落库。

几个工程上的取舍：

**按章并发、章内串行**。章与章之间没有依赖，并发能压掉一半以上时间；
章内串行是为了让"本章已知人名"逐步累积——同一章的称谓通常在后半段才统一，
前一块抽到"曹操"，后一块就别再抽成"曹阿瞒"。

**重跑直接清空重建**，不做增量 diff。抽取结果依赖 prompt 与模型版本，
这两者一变，旧的边就不再可比较；而图谱是全量可重建的派生数据（正文才是事实源），
清空重来的代价只是再烧一遍 LLM，比"新旧口径混在一张表里"划算。

**mention 计数在对齐后统一回扫**。抽取时统计会漏掉"抽到了人但这一句没抽到"的情况，
而 mention_count 直接决定前端节点大小和 importance 排序，值得多扫一遍：
551 块 × 千级实体在 Python 里也就几百毫秒。
"""

from __future__ import annotations

import asyncio
import logging
import math
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import SessionLocal
from app.models import Chunk, Entity, EntityMention, Job, Novel, Relation, new_id
from app.services.graph import candidates as cand
from app.services.graph.aligner import AlignResult, align
from app.services.graph.extractor import ChunkExtraction, ExtractedEntity, ExtractedRelation, extract_chunk
from app.services.llm.client import get_llm
from app.workers.cancel import IngestCancelled, JobCancelled, is_cancelled, is_job_cancelled

logger = logging.getLogger(__name__)

# mention 只统计这四类：Event 的名字（"桃园结义"）多半是抽取时概括的动宾短语，
# 正文里不会原样出现；Concept 同理。硬统计只会得到一堆 0。
MENTION_TYPES = ("Character", "Faction", "Location", "Item")


@dataclass
class _Block:
    chunk_id: str
    chapter_no: int
    chapter_title: str
    seq: int
    content: str


@dataclass
class _Acc:
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    stats: dict = field(default_factory=lambda: defaultdict(int))

    def merge(self, res: ChunkExtraction, chunk_id: str, chapter_no: int) -> None:
        self.entities.extend(res.entities)
        for r in res.relations:
            r.chunk_id = chunk_id
            r.chapter_no = chapter_no
            self.relations.append(r)
        for k, v in res.stats.items():
            self.stats[k] += v


@dataclass
class GraphBuildStats:
    blocks: int = 0
    entities: int = 0
    relations: int = 0
    active: int = 0
    pending: int = 0
    mentions: int = 0
    merged_pairs: int = 0
    llm_calls: int = 0
    llm_errors: int = 0
    dropped_no_evidence: int = 0
    evidence_partial: int = 0
    other_relation: int = 0
    duration_sec: float = 0.0

    def as_dict(self) -> dict:
        return {
            "blocks": self.blocks,
            "entities": self.entities,
            "relations": self.relations,
            "active_relations": self.active,
            "pending_relations": self.pending,
            "mentions": self.mentions,
            "merged_pairs": self.merged_pairs,
            "llm_calls": self.llm_calls,
            "llm_errors": self.llm_errors,
            "dropped_no_evidence": self.dropped_no_evidence,
            "evidence_partial": self.evidence_partial,
            "other_relation": self.other_relation,
            "duration_sec": self.duration_sec,
        }


async def _load_blocks(novel_id: str) -> list[_Block]:
    """优先用 parent 块抽：上下文完整、调用次数少。

    没有 parent（切片参数改过或更老的数据）时退到 child，保证能跑。
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Chunk)
                .where(Chunk.novel_id == novel_id, Chunk.level == 0)
                .order_by(Chunk.chapter_no, Chunk.seq)
            )
        ).scalars().all()
        if not rows:
            rows = (
                await db.execute(
                    select(Chunk)
                    .where(Chunk.novel_id == novel_id, Chunk.level == 1)
                    .order_by(Chunk.chapter_no, Chunk.seq)
                )
            ).scalars().all()
        return [
            _Block(
                chunk_id=r.id,
                chapter_no=r.chapter_no,
                chapter_title=r.title or "",
                seq=r.seq,
                content=r.content,
            )
            for r in rows
        ]


async def _progress(job_id: str, **fields) -> None:  # noqa: ANN003
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            return
        for k, v in fields.items():
            setattr(job, k, v)
        await db.commit()


async def build_graph(job_id: str, *, novel_id: str | None = None) -> dict:
    """构建一本书的图谱。进度写进 job 表，前端轮询同一个接口即可。"""
    started = time.monotonic()
    stats = GraphBuildStats()

    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job is None:
            logger.error("graph job %s 不存在", job_id)
            return stats.as_dict()
        novel_id = novel_id or job.novel_id
        novel = await db.get(Novel, novel_id)
        if novel is None:
            job.status = "FAILED"
            job.error = "小说记录不存在"
            await db.commit()
            return stats.as_dict()
        job.status = "RUNNING"
        job.kind = "graph"
        job.stage = "GRAPH_EXTRACTING"
        job.progress = 0.0
        job.chars_total = novel.char_count
        job.started_at = datetime.now(UTC)
        await db.commit()

    try:
        await _run(job_id, novel_id, stats, started)
    except (IngestCancelled, JobCancelled):
        logger.warning("graph 构建被取消 novel=%s", novel_id)
        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            if job:
                job.status = "CANCELED"
                job.stage = "CANCELED"
                job.finished_at = datetime.now(UTC)
            await db.commit()
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("graph build failed novel=%s", novel_id)
        async with SessionLocal() as db:
            job = await db.get(Job, job_id)
            if job:
                job.status = "FAILED"
                job.stage = "FAILED"
                job.error = f"{type(exc).__name__}: {exc}"[:2000]
                job.finished_at = datetime.now(UTC)
            await db.commit()
        return stats.as_dict()

    return stats.as_dict()


async def _run(job_id: str, novel_id: str, stats: GraphBuildStats, started: float) -> None:
    blocks = await _load_blocks(novel_id)
    stats.blocks = len(blocks)
    if not blocks:
        await _progress(job_id, status="FAILED", stage="FAILED", error="没有可抽取的切片，请先入库")
        return

    by_chapter: dict[int, list[_Block]] = defaultdict(list)
    for b in blocks:
        by_chapter[b.chapter_no].append(b)
    chapter_titles = {b.chapter_no: b.chapter_title for b in blocks}

    # L1：整章正文扫一遍候选名（纯 CPU，一次扫完，避免逐块重复扫）
    chapter_known = {no: cand.extract_candidates("".join(b.content for b in bl), top_n=40) for no, bl in by_chapter.items()}

    acc = _Acc()
    done = 0
    progress_lock = asyncio.Lock()
    sem = asyncio.Semaphore(max(1, min(settings.graph_concurrency, len(by_chapter))))
    llm = get_llm()

    await _progress(job_id, stage="GRAPH_EXTRACTING", chapters_total=len(by_chapter))

    async def run_chapter(no: int, chapter_blocks: list[_Block]) -> None:
        nonlocal done
        known: list[str] = list(chapter_known.get(no, []))
        title = chapter_titles.get(no, "")
        for b in chapter_blocks:
            if is_cancelled(novel_id):
                raise IngestCancelled(novel_id)
            if is_job_cancelled(job_id):
                raise JobCancelled(job_id)
            res = await extract_chunk(llm, b.content, chapter_no=no, chapter_title=title, known_names=known)
            acc.merge(res, b.chunk_id, no)
            stats.llm_calls += 1
            if res.stats.get("llm_error"):
                stats.llm_errors += 1
            # 抽到的人名回灌给同章后续块，抑制称谓分裂
            for e in res.entities:
                if e.type == "Character" and e.name not in known:
                    known.append(e.name)
            async with progress_lock:
                done += 1
                await _progress(
                    job_id,
                    progress=round(85.0 * done / max(stats.blocks, 1), 1),
                    chunks_created=done,
                    chapters_done=no,
                )

    async def guarded(no: int, bl: list[_Block]) -> None:
        async with sem:
            try:
                await run_chapter(no, bl)
            except (IngestCancelled, JobCancelled):
                raise
            except Exception:  # noqa: BLE001
                logger.warning("chapter %s 抽取失败，跳过", no, exc_info=True)
                stats.llm_errors += 1

    await asyncio.gather(*(guarded(no, bl) for no, bl in sorted(by_chapter.items())))

    # ---------- L3 对齐 ----------
    await _progress(job_id, stage="GRAPH_ALIGNING", progress=88.0)
    aligned = align(acc.entities)
    stats.merged_pairs = len(aligned.merged_pairs)

    # ---------- mention 回扫 + importance ----------
    mention_rows: list[dict] = []
    totals: dict[tuple[str, str], int] = defaultdict(int)
    chapter_hits: dict[tuple[str, str], set[int]] = defaultdict(set)
    mention_entities = [e for e in aligned.entities if e.type in MENTION_TYPES]
    for b in blocks:
        for ent in mention_entities:
            n = 0
            for name in (ent.name, *ent.aliases):
                if len(name) >= 2:
                    n += b.content.count(name)
            if n:
                key = (ent.type, ent.name)
                totals[key] += n
                chapter_hits[key].add(b.chapter_no)
                mention_rows.append(
                    {"_key": key, "novel_id": novel_id, "chapter_no": b.chapter_no, "chunk_id": b.chunk_id, "count": n}
                )

    for ent in aligned.entities:
        key = (ent.type, ent.name)
        if key in totals:
            ent.mention_count = totals[key]
            ent.first_chapter = min(chapter_hits[key])

    if aligned.entities:
        # log 缩放：主角出现上千次、路人两次，线性归一会把所有配角压成同一个点
        peak = max((e.mention_count for e in aligned.entities), default=1) or 1
        for e in aligned.entities:
            e.importance = round(math.log1p(e.mention_count) / math.log1p(peak), 4)

    # ---------- 关系解析与去重 ----------
    names_by_type: dict[str, dict[str, str]] = defaultdict(dict)
    for e in aligned.entities:
        names_by_type[e.type][e.name] = e.name
        for a in e.aliases:
            names_by_type[e.type][a] = e.name

    def resolve(name: str) -> tuple[str, str] | None:
        """名字 → (type, 规范名)。同名跨类型且无法判定时返回 None（宁缺勿错）。"""
        if not name:
            return None
        hits = [(t, table[name]) for t, table in names_by_type.items() if name in table]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            return None
        # 多类型同名：人物优先，其次势力/地点
        hits.sort(key=lambda x: ("Character", "Faction", "Location").index(x[0]) if x[0] in ("Character", "Faction", "Location") else 9)
        return hits[0]

    best: dict[tuple[tuple[str, str], tuple[str, str], str], ExtractedRelation] = {}
    for r in acc.relations:
        src, dst = resolve(r.src), resolve(r.dst)
        if not src or not dst or src == dst:
            continue
        key = (src, dst, r.relation)
        cur = best.get(key)
        if cur is None or (r.confidence, len(r.evidence)) > (cur.confidence, len(cur.evidence)):
            best[key] = r

    stats.dropped_no_evidence = acc.stats.get("dropped_no_evidence", 0)
    stats.evidence_partial = acc.stats.get("evidence_partial", 0)
    stats.other_relation = acc.stats.get("other_relation", 0)
    stats.entities = len(aligned.entities)
    stats.relations = len(best)
    stats.active = sum(1 for r in best.values() if r.status == "active")
    stats.pending = stats.relations - stats.active

    # ---------- 落库 ----------
    await _progress(job_id, stage="GRAPH_WRITING", progress=92.0)
    stats.mentions = await _persist(novel_id, aligned, best, mention_rows)

    stats.duration_sec = round(time.monotonic() - started, 1)
    async with SessionLocal() as db:
        job = await db.get(Job, job_id)
        if job:
            job.status = "COMPLETED"
            job.stage = "COMPLETED"
            job.progress = 100.0
            job.chapters_done = len(by_chapter)
            job.vectors_written = stats.relations
            job.chunks_created = stats.entities
            job.finished_at = datetime.now(UTC)
            job.stats = stats.as_dict()
        novel = await db.get(Novel, novel_id)
        if novel:
            meta = dict(novel.meta or {})
            meta["graph"] = {
                "entities": stats.entities,
                "relations": stats.relations,
                "built_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            novel.meta = meta
        await db.commit()

    logger.info(
        "graph done novel=%s blocks=%s entities=%s relations=%s (%s active / %s pending) %.1fs",
        novel_id,
        stats.blocks,
        stats.entities,
        stats.relations,
        stats.active,
        stats.pending,
        stats.duration_sec,
    )


async def _persist(
    novel_id: str,
    aligned: AlignResult,
    relations: dict[tuple[tuple[str, str], tuple[str, str], str], ExtractedRelation],
    mention_rows: list[dict],
) -> int:
    """先清后写，返回写入的 mention 行数。"""
    async with SessionLocal() as db:
        # 按外键反向删：mentions → relations → entities
        await db.execute(delete(EntityMention).where(EntityMention.novel_id == novel_id))
        await db.execute(delete(Relation).where(Relation.novel_id == novel_id))
        await db.execute(delete(Entity).where(Entity.novel_id == novel_id))
        await db.commit()

        if not aligned.entities:
            return 0

        id_map: dict[tuple[str, str], str] = {}
        ent_rows = []
        for e in aligned.entities:
            eid = new_id("ent")
            id_map[(e.type, e.name)] = eid
            ent_rows.append(
                {
                    "id": eid,
                    "novel_id": novel_id,
                    "name": e.name,
                    "type": e.type,
                    "aliases": e.aliases,
                    "desc": e.desc[:500],
                    "first_chapter": e.first_chapter,
                    "importance": e.importance,
                    "mention_count": e.mention_count,
                    "meta": {},
                }
            )
        stmt = pg_insert(Entity).on_conflict_do_nothing(constraint="uq_entities_novel_type_name")
        await db.execute(stmt, ent_rows)

        rel_rows = []
        for (src_key, dst_key, relation), r in relations.items():
            src_id, dst_id = id_map.get(src_key), id_map.get(dst_key)
            if not src_id or not dst_id:
                continue
            rel_rows.append(
                {
                    "id": new_id("rel"),
                    "novel_id": novel_id,
                    "src_id": src_id,
                    "dst_id": dst_id,
                    "relation": relation,
                    "raw_relation": r.raw_relation,
                    "strength": r.strength,
                    "confidence": r.confidence,
                    "status": r.status,
                    "evidence": r.evidence,
                    "chapter_no": r.chapter_no,
                    "chunk_id": r.chunk_id or None,
                }
            )
        if rel_rows:
            await db.execute(pg_insert(Relation).on_conflict_do_nothing(constraint="uq_relations_edge"), rel_rows)

        men_rows = []
        for row in mention_rows:
            eid = id_map.get(row["_key"])
            if not eid:
                continue
            men_rows.append(
                {
                    "id": new_id("men"),
                    "novel_id": novel_id,
                    "entity_id": eid,
                    "chapter_no": row["chapter_no"],
                    "chunk_id": row["chunk_id"],
                    "count": row["count"],
                }
            )
        if men_rows:
            await db.execute(
                pg_insert(EntityMention).on_conflict_do_nothing(constraint="uq_mentions_entity_chunk"),
                men_rows,
            )

        await db.commit()
        return len(men_rows)
