"""把历史数据从 SQLite + Milvus 迁到 PostgreSQL + pgvector。

为什么要有这个脚本
------------------
《赘婿》一本书 21552 个切片，1024 维向量化要跑两个多小时。换存储不等于要重算——
Milvus 里现成的向量直接搬进 pgvector 就行，只有真正缺的部分才需要重跑
（入库的断点续跑会自动补，见 `rebuild?mode=vectors`）。

做四件事
--------
1. SQLite 元数据 → PG：novels / chapters / chunks / chunk_reports / jobs / chat_*
2. Milvus 向量 → `chunk_vectors`（只搬 PG 里确实存在的 chunk，避免外键失败）
3. 重建词法索引（tsvector）
4. 打印对账结果：每本书的 chunk 数 vs 向量数

用法
----
    cd backend
    uv run python scripts/migrate_to_pg.py                # 全量
    uv run python scripts/migrate_to_pg.py --skip-vectors  # 只搬元数据
    uv run python scripts/migrate_to_pg.py --force         # PG 里已有数据也重来

前置条件：目标库已建好并 `CREATE EXTENSION vector`（见 tools/install_pgvector.cmd）。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

from sqlalchemy import String, Text, func, select, text  # noqa: E402
from sqlalchemy.dialects.postgresql import insert as pg_insert  # noqa: E402

from app.config import BASE_DIR, settings  # noqa: E402
from app.db import SessionLocal, engine, init_db  # noqa: E402
from app.models import (  # noqa: E402
    Chapter,
    ChatMessage,
    ChatSession,
    Chunk,
    ChunkReport,
    ChunkVector,
    Job,
    Novel,
)
from app.services.retrieval import lexical  # noqa: E402

SQLITE_PATH = BASE_DIR / "data" / "app.db"

# SQLite 表 → ORM 模型。顺序即插入顺序（有外键的在后）。
MODELS: list[tuple[str, Any]] = [
    ("novels", Novel),
    ("chapters", Chapter),
    ("chunks", Chunk),
    ("chunk_reports", ChunkReport),
    ("jobs", Job),
    ("chat_sessions", ChatSession),
    ("chat_messages", ChatMessage),
]

JSON_COLUMNS = {"meta", "stats", "citations"}
DATETIME_COLUMNS = {
    "created_at",
    "updated_at",
    "started_at",
    "finished_at",
}


def parse_dt(v: Any) -> datetime | None:
    """SQLite 存的是 naive UTC 字符串，PG 列是 timestamptz —— 必须补上 UTC 再写。"""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v if v.tzinfo else v.replace(tzinfo=UTC)
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(float(v), UTC)
    s = str(v).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        try:
            dt = datetime.strptime(s.split(".")[0], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def parse_json(v: Any, default: Any) -> Any:
    if v is None:
        return default
    if isinstance(v, (dict, list)):
        return v
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return default


def coerce(row: dict, model: Any) -> dict:
    """SQLite 行 → 可以直接喂给 PG insert 的 dict。"""
    cols = model.__table__.columns
    out: dict = {}
    for k, v in row.items():
        if k not in cols:
            continue
        col = cols[k]
        if k in DATETIME_COLUMNS:
            out[k] = parse_dt(v)
        elif k in JSON_COLUMNS:
            out[k] = parse_json(v, [] if k == "citations" else {})
        elif v is None and not col.nullable:
            # 老库里少量列可能为 NULL（早期 schema 允许），补默认值避免 NOT NULL 报错
            out[k] = "" if isinstance(col.type, (String, Text)) else 0
        else:
            out[k] = v
    return out


async def _table_count(model: Any) -> int:
    async with SessionLocal() as db:
        return int((await db.execute(select(func.count()).select_from(model))).scalar() or 0)


async def migrate_metadata(sqlite_path: Path, force: bool) -> dict[str, int]:
    if not sqlite_path.exists():
        raise SystemExit(f"找不到 SQLite 库：{sqlite_path}")

    existing = await _table_count(Novel)
    if existing and not force:
        print(f"[跳过] PG 里已有 {existing} 本书。要重来请加 --force（会先清空目标表）")
        return {}

    con = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    counts: dict[str, int] = {}

    if force and existing:
        async with SessionLocal() as db:
            # 顺序：子表在前，父表在后（虽然都是 CASCADE，显式点更好排查）
            for tbl in ("chunk_lexical", "chunk_vectors", "chunk_reports", "chat_messages", "chat_sessions", "jobs", "chunks", "chapters", "novels"):
                await db.execute(text(f"DELETE FROM {tbl}"))
            await db.commit()
        print("[清理] 目标表已清空")

    for table, model in MODELS:
        try:
            rows = [dict(r) for r in con.execute(f"SELECT * FROM {table}").fetchall()]
        except sqlite3.OperationalError as exc:
            print(f"[跳过] {table}: {exc}")
            continue
        if not rows:
            counts[table] = 0
            continue

        payload = [coerce(r, model) for r in rows]
        async with SessionLocal() as db:
            for start in range(0, len(payload), 2000):
                part = payload[start : start + 2000]
                # 不带 index_elements 的 DO NOTHING 会匹配任意唯一冲突，够用且不依赖列名
                stmt = pg_insert(model.__table__).on_conflict_do_nothing()
                await db.execute(stmt, part)
                await db.commit()
        counts[table] = len(payload)
        print(f"[迁移] {table:16} {len(payload):>7} 行")

    con.close()
    return counts


# ---------------------------------------------------------------- Milvus 向量
def _load_env_file() -> None:
    """pydantic-settings 把 .env 读进了 Settings 对象，不会写回 os.environ。

    Milvus 相关配置已经从 Settings 里移除（运行期不再用），但迁移脚本还得读它们，
    所以这里单独解析一次 .env，且不覆盖已有的环境变量。
    """
    path = BASE_DIR / ".env"
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def _milvus_client() -> Any | None:
    uri = (os.environ.get("MILVUS_URI") or "").strip()
    token = (os.environ.get("MILVUS_TOKEN") or "").strip()
    if not uri or "://" not in uri or not token:
        print("[跳过] MILVUS_URI / MILVUS_TOKEN 未配置，不迁向量")
        return None
    try:
        from pymilvus import MilvusClient
    except ImportError:
        print("[跳过] 未安装 pymilvus（uv sync --dev 可装），不迁向量")
        return None
    return MilvusClient(
        uri=uri, token=token, db_name=os.environ.get("MILVUS_DB_NAME", "default"), timeout=30
    )


async def migrate_vectors(batch: int = 400) -> int:
    client = _milvus_client()
    if client is None:
        return 0

    collection = os.environ.get("MILVUS_COLLECTION", "novel_chunks_d1024")
    async with SessionLocal() as db:
        novels = [r[0] for r in (await db.execute(select(Novel.id))).all()]
    print(f"[向量] 目标 {len(novels)} 本书，collection={collection}")

    total = 0
    for nid in novels:
        async with SessionLocal() as db:
            chunk_ids = set(
                (await db.execute(select(Chunk.id).where(Chunk.novel_id == nid, Chunk.level == 1))).scalars().all()
            )
            have = set(
                (await db.execute(select(ChunkVector.chunk_id).where(ChunkVector.novel_id == nid))).scalars().all()
            )
        if not chunk_ids:
            continue

        buf: list[dict] = []
        moved = 0
        try:
            it = client.query_iterator(
                collection_name=collection,
                batch_size=500,
                filter=f'novel_id == "{nid}"',
                output_fields=["id", "dense"],
            )
            while True:
                part = it.next()
                if not part:
                    break
                for r in part:
                    cid = str(r["id"])
                    if cid not in chunk_ids or cid in have:
                        continue  # 孤儿向量、或者已经搬过
                    buf.append({"chunk_id": cid, "novel_id": nid, "embedding": list(r["dense"])})
                    if len(buf) >= batch:
                        moved += await _flush_vectors(buf)
                        buf.clear()
            it.close()
        except Exception as exc:  # noqa: BLE001
            print(f"[警告] {nid} 读取 Milvus 失败：{type(exc).__name__}: {exc}")
        if buf:
            moved += await _flush_vectors(buf)

        total += moved
        print(f"[向量] {nid}: 搬入 {moved} 条（chunk {len(chunk_ids)} / 已有 {len(have)}）")

    return total


async def _flush_vectors(buf: list[dict]) -> int:
    sql = text(
        "INSERT INTO chunk_vectors(chunk_id, novel_id, embedding) "
        "VALUES (:chunk_id, :novel_id, CAST(:vec AS vector)) "
        "ON CONFLICT (chunk_id) DO NOTHING"
    )
    rows = [
        {
            "chunk_id": b["chunk_id"],
            "novel_id": b["novel_id"],
            "vec": "[" + ",".join(f"{x:.6f}" for x in b["embedding"]) + "]",
        }
        for b in buf
    ]
    async with SessionLocal() as db:
        await db.execute(sql, rows)
        await db.commit()
    return len(rows)


# ---------------------------------------------------------------- 收尾
async def rebuild_lexical() -> int:
    async with SessionLocal() as db:
        novels = [r[0] for r in (await db.execute(select(Novel.id))).all()]
    n = 0
    for nid in novels:
        n += await lexical.reindex_novel(nid)
    return n


async def report() -> None:
    print("\n=== 对账 ===")
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """
                    SELECT n.title,
                           (SELECT count(*) FROM chunks c WHERE c.novel_id = n.id AND c.level = 1) AS chunks,
                           (SELECT count(*) FROM chunk_vectors v WHERE v.novel_id = n.id) AS vectors,
                           (SELECT count(*) FROM chunk_lexical l WHERE l.novel_id = n.id) AS lex
                    FROM novels n ORDER BY n.created_at
                    """
                )
            )
        ).all()
    if not rows:
        print("目标库是空的")
        return
    print(f"{'书名':24} {'切片':>8} {'向量':>8} {'词法':>8}  状态")
    for r in rows:
        gap = int(r.chunks) - int(r.vectors)
        flag = "完整" if gap == 0 and int(r.lex) == int(r.chunks) else f"缺向量 {gap}"
        title = (r.title or "")[:22]
        print(f"{title:24} {r.chunks:>8} {r.vectors:>8} {r.lex:>8}  {flag}")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sqlite", default=str(SQLITE_PATH))
    ap.add_argument("--skip-vectors", action="store_true")
    ap.add_argument("--skip-lexical", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    _load_env_file()
    print(f"目标库：{settings.database_url.split('@')[-1]}")
    await init_db()
    print("[建表] 完成")

    await migrate_metadata(Path(args.sqlite), args.force)

    if not args.skip_vectors:
        n = await migrate_vectors()
        print(f"[向量] 合计搬入 {n} 条")

    if not args.skip_lexical:
        n = await rebuild_lexical()
        print(f"[词法] 重建 {n} 条倒排")

    await report()
    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
