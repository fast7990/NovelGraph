"""pgvector 适配器（替代 Milvus）。

和 Milvus 版本的三个关键差异
------------------------------
1. **向量和正文同库**，`chunk_vectors.chunk_id` 外键指向 `chunks`，
   `ON DELETE CASCADE` 让"删书"自动清掉向量 —— Milvus 时代要靠应用层记得删，
   删漏了就变成孤儿向量，worker 还会继续给一本不存在的书算向量（真出现过）。
2. **HNSW + 过滤条件**：`WHERE novel_id = ?` 会让 HNSW 先在全局取 ef_search 个候选、
   再过滤，检索单本书时经常召回不足（10 条都凑不齐）。pgvector 0.8 的
   `hnsw.iterative_scan` 就是解决这个的，本文件按配置打开。
3. **维度由列类型兜底**：`vector(1024)` 是硬约束，插 512 维 PG 直接拒，
   不再依赖应用层比对 —— 但换模型仍要重建表，所以 ensure_ready 会显式校验并报错。
"""

from __future__ import annotations

import logging
import time
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.config import settings
from app.db import SessionLocal
from app.models import ChunkVector
from app.services.vector.base import VectorHit

logger = logging.getLogger(__name__)


class PgVectorStore:
    name = "pgvector"

    def __init__(self) -> None:
        self.table = settings.pgvector_table
        self._ready = False
        self._dim = settings.embedding_dim
        self._index_ready = False

    # ---------- 建表 / 索引 ----------
    async def ensure_ready(self, dim: int) -> dict:
        if self._ready and self._dim == dim:
            return {"backend": self.name, "dim": dim, "ready": True}

        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    text(
                        "SELECT format_type(atttypid, atttypmod) AS t FROM pg_attribute "
                        "WHERE attrelid = 'chunk_vectors'::regclass AND attname = 'embedding'"
                    )
                )
            ).first()
            if rows is None:
                raise RuntimeError("chunk_vectors 表不存在，请确认已执行 init_db()")

            col_type = str(rows[0])  # 形如 vector(1024)
            stored_dim = int(col_type.split("(")[-1].rstrip(")")) if "(" in col_type else 0
            if stored_dim and stored_dim != dim:
                raise RuntimeError(
                    f"chunk_vectors.embedding 是 {col_type}，当前 Embedding 输出 {dim} 维。"
                    "向量维度不能原地修改，必须重建这张表：\n"
                    "  TRUNCATE chunk_vectors;\n"
                    f"  ALTER TABLE chunk_vectors ALTER COLUMN embedding TYPE vector({dim});\n"
                    "然后对每本书执行 rebuild?mode=vectors 重算。"
                )

            existing = (
                await db.execute(text("SELECT count(*) FROM chunk_vectors"))
            ).scalar() or 0

            if settings.pgvector_index in ("hnsw", "ivfflat"):
                await self._ensure_index(db, settings.pgvector_index)
            await db.commit()

        self._dim, self._ready = dim, True
        logger.info(
            "pgvector ready: table=%s dim=%s rows=%s index=%s",
            self.table,
            dim,
            existing,
            settings.pgvector_index,
        )
        return {"backend": self.name, "dim": dim, "rows": int(existing), "ready": True}

    async def _ensure_index(self, db: Any, kind: str) -> None:
        """建向量索引。已存在就跳过（IF NOT EXISTS 不带参数块，不能建错参数）。"""
        idx_name = f"ix_{self.table}_{kind}"
        has = (
            await db.execute(text("SELECT 1 FROM pg_class WHERE relname = :n"), {"n": idx_name})
        ).scalar()
        if has:
            self._index_ready = True
            return

        t0 = time.monotonic()
        if kind == "hnsw":
            ddl = (
                f"CREATE INDEX IF NOT EXISTS {idx_name} ON {self.table} "
                f"USING hnsw (embedding vector_cosine_ops) "
                f"WITH (m = {int(settings.pgvector_hnsw_m)}, "
                f"ef_construction = {int(settings.pgvector_hnsw_ef_construction)})"
            )
        else:
            # IVFFlat 必须有数据才能算出合理的聚类中心，空表建了也没用
            ddl = (
                f"CREATE INDEX IF NOT EXISTS {idx_name} ON {self.table} "
                "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
            )
        await db.execute(text(ddl))
        await db.commit()
        self._index_ready = True
        logger.info("pgvector 索引 %s 建好，耗时 %.1fs", idx_name, time.monotonic() - t0)

    # ---------- 写入 ----------
    async def upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        payload = [
            {"chunk_id": r["id"], "novel_id": r["novel_id"], "embedding": list(r["dense"])}
            for r in rows
        ]
        async with SessionLocal() as db:
            stmt: Any = pg_insert(ChunkVector)
            stmt = stmt.on_conflict_do_update(
                index_elements=[ChunkVector.chunk_id],
                set_={"embedding": stmt.excluded.embedding, "novel_id": stmt.excluded.novel_id},
            )
            try:
                await db.execute(stmt, payload)
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                # 外键失败基本只有一个原因：chunk 正文还没落库。整批重试没意义，
                # 直接把第一责任人指出来，比让 PG 抛 constraint 名好排查。
                if "fk_chunk_vectors_chunk" in str(exc):
                    raise RuntimeError(
                        "向量写入失败：存在 chunk 正文尚未写入 chunks 表。"
                        "入库顺序必须是「先写 chunks，再写向量」。"
                    ) from exc
                raise
        return len(payload)

    async def delete_novel(self, novel_id: str) -> None:
        async with SessionLocal() as db:
            await db.execute(
                text(f"DELETE FROM {self.table} WHERE novel_id = :nid"), {"nid": novel_id}
            )
            await db.commit()

    async def existing_ids(self, novel_id: str) -> set[str]:
        """已入库的 chunk id，用于断点续跑。

        PG 没有 Milvus 那个 16384 的 offset 上限，两万条一次取回也就几 MB，
        直接一次查完，省掉游标状态。
        """
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    text(f"SELECT chunk_id FROM {self.table} WHERE novel_id = :nid"),
                    {"nid": novel_id},
                )
            ).scalars().all()
        return set(rows)

    # ---------- 查询 ----------
    async def _set_search_params(self, db: Any) -> None:
        """查询期参数。用 SET LOCAL，只在当前事务生效，不污染连接池里的其他连接。"""
        if settings.pgvector_index != "hnsw":
            return
        try:
            await db.execute(text(f"SET LOCAL hnsw.ef_search = {int(settings.pgvector_hnsw_ef_search)}"))
            scan = (settings.pgvector_iterative_scan or "off").strip()
            if scan in ("relaxed_order", "strict_order"):
                await db.execute(text(f"SET LOCAL hnsw.iterative_scan = {scan}"))
        except Exception as exc:  # noqa: BLE001
            # 老版本 pgvector 没有 iterative_scan 这个参数，不影响主查询
            logger.debug("设置 HNSW 查询参数失败（可忽略）：%s", exc)

    async def search(
        self, novel_id: str, vector: list[float], top_k: int, level: int = 1
    ) -> list[VectorHit]:
        sql = text(
            f"""
            SELECT c.id, c.novel_id, c.level, c.chapter_no, c.seq, c.parent_id,
                   c.title, c.content, c.char_start, c.char_end,
                   1 - (v.embedding <=> CAST(:q AS vector)) AS score
            FROM {self.table} v
            JOIN chunks c ON c.id = v.chunk_id
            WHERE v.novel_id = :nid AND c.level = :lv
            ORDER BY v.embedding <=> CAST(:q AS vector)
            LIMIT :k
            """
        )
        # 向量以字面量传入：1024 维约 12KB，比注册 asyncpg codec 少一层坑
        # （SQLAlchemy 的 asyncpg 方言在 connect 阶段没法 await 注册类型）
        q = "[" + ",".join(f"{x:.6f}" for x in vector) + "]"
        async with SessionLocal() as db:
            await self._set_search_params(db)
            rows = (
                await db.execute(sql, {"q": q, "nid": novel_id, "lv": level, "k": top_k})
            ).all()
            await db.commit()
        return [
            VectorHit(
                id=r.id,
                score=float(r.score),
                novel_id=r.novel_id,
                level=int(r.level),
                chapter_no=int(r.chapter_no),
                seq=int(r.seq),
                parent_id=r.parent_id or "",
                title=r.title or "",
                content=r.content or "",
                char_start=int(r.char_start),
                char_end=int(r.char_end),
            )
            for r in rows
        ]

    async def count(self, novel_id: str | None = None) -> int:
        async with SessionLocal() as db:
            if novel_id:
                n = (
                    await db.execute(
                        text(f"SELECT count(*) FROM {self.table} WHERE novel_id = :nid"),
                        {"nid": novel_id},
                    )
                ).scalar()
            else:
                n = (await db.execute(text(f"SELECT count(*) FROM {self.table}"))).scalar()
        return int(n or 0)

    async def sample(self, novel_id: str, limit: int = 500) -> list[VectorHit]:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    text(
                        f"""
                        SELECT c.id, c.novel_id, c.level, c.chapter_no, c.seq, c.parent_id,
                               c.title, c.char_start, c.char_end
                        FROM {self.table} v JOIN chunks c ON c.id = v.chunk_id
                        WHERE v.novel_id = :nid LIMIT :lim
                        """
                    ),
                    {"nid": novel_id, "lim": limit},
                )
            ).all()
        return [
            VectorHit(
                id=r.id,
                score=0.0,
                novel_id=r.novel_id,
                level=int(r.level),
                chapter_no=int(r.chapter_no),
                seq=int(r.seq),
                parent_id=r.parent_id or "",
                title=r.title or "",
                content="",
                char_start=int(r.char_start),
                char_end=int(r.char_end),
            )
            for r in rows
        ]

    async def fetch_vectors(self, novel_id: str) -> list[dict]:
        """取回整本书的向量，供 UMAP 降维可视化用（P1-M9）。"""
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    text(
                        f"SELECT chunk_id, embedding FROM {self.table} WHERE novel_id = :nid LIMIT 5000"
                    ),
                    {"nid": novel_id},
                )
            ).all()
        return [{"chunk_id": r.chunk_id, "embedding": r.embedding} for r in rows]

    async def health(self) -> dict:
        try:
            async with SessionLocal() as db:
                ver = (await db.execute(text("SELECT extversion FROM pg_extension WHERE extname='vector'"))).scalar()
                rows = (await db.execute(text(f"SELECT count(*) FROM {self.table}"))).scalar() or 0
                idx = (
                    await db.execute(
                        text(
                            "SELECT indexname FROM pg_indexes "
                            "WHERE tablename = :t AND indexdef LIKE '%hnsw%' OR "
                            "tablename = :t AND indexdef LIKE '%ivfflat%' LIMIT 1"
                        ),
                        {"t": self.table},
                    )
                ).scalar()
            return {
                "name": "pgvector",
                "status": "ok",
                "server_version": f"pgvector {ver}",
                "collection": self.table,
                "dim": self._dim,
                "rows": int(rows),
                "index": idx or "无（顺序扫描）",
            }
        except Exception as exc:  # noqa: BLE001
            return {"name": "pgvector", "status": "down", "detail": str(exc)}


__all__ = ["PgVectorStore"]
