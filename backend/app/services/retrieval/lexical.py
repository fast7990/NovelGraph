"""词法检索通道：PostgreSQL 全文索引 + 自建 bigram 分词。

这条通道换过两次实现，核心难点始终是**中文两字词**：
- 最初用 SQLite FTS5 的 `trigram` 分词器 → `MATCH '"铜灯"'` 直接返回空，
  因为 trigram 只对 ≥3 字的片段建索引，而小说里最值钱的恰恰是人名、物名这两字词；
- 现在换 PG，默认分词器同样不会切中文（`to_tsvector('simple','陆沉把刀')`
  很可能整段算一个 token），所以仍然沿用 bigram 预切分。

做法（与存储引擎无关，只依赖一列倒排）
--------------------------------------
把正文切成 bigram 序列（“陆沉把刀” → `陆沉 沉把 把刀`），空格分隔后
交给 `to_tsvector('simple', ...)` 建倒排；查询同样转 bigram 后用 `|` 连接，
`ts_rank_cd` 排序。这样：
- 两字词命中：切完就是一个 token，精确匹配；
- 长查询不要求全中：OR 天然做部分匹配加权；
- 不引 jieba：少一个依赖，也不会有"索引切词 / 查询切词"两套词典漂移的问题。

**为什么不像 PG 常见做法那样直接用 `to_tsvector('simple', content)`**
因为那要求数据库里有中文分词器（zhparser / pg_jieba），都要额外编译。
在 Windows 上装一个 pgvector 已经够折腾了，不值得为分词再加一个编译项。

启动时会做一次自检（`_probe_cjk`）：如果 PG 把带空格的 bigram 串当成单个 token，
说明这套假设不成立，词法通道会主动关闭并在 `/health` 里标红，
检索退化到 `dense_only` —— 宁可少一条通道，也不要返回错的排序。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import SessionLocal

logger = logging.getLogger(__name__)

TABLE = "chunk_lexical"

# 只保留 CJK / 字母 / 数字，其余（标点、空白、符号）当作分隔符
_KEEP = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbfa-zA-Z0-9]+")
# 高频虚词：由它们组成的 bigram 对区分度没有贡献，反而把结果拉平
_STOP_CHARS = set("的了是在和就都很也不我你他她它这那有与及而或者之其一个上下来去到说要会能把被给对从为以于")
# 单条查询最多带多少个 bigram 进 tsquery，防止超长问题把 SQL 撑爆
_MAX_TERMS = 48

# CJK 分词自检结果：True = tsquery 可用，False = 只能放弃词法通道
_cjk_ok: bool | None = None


def _segments(text_: str) -> list[str]:
    return _KEEP.findall(text_ or "")


def bigram_string(content: str) -> str:
    """正文 → "aa bb cc" 形式的 bigram 串。"""
    out: list[str] = []
    for seg in _segments(content):
        if len(seg) == 1:
            # 单字段落（比如独立的“刀”）单独留一个 token，否则整段会被丢掉
            out.append(seg)
            continue
        for i in range(len(seg) - 1):
            out.append(seg[i : i + 2])
    return " ".join(out)


def build_match_query(question: str) -> str | None:
    """问题 → tsquery 表达式（bigram 用 | 连接）。None 表示这问题没法走词法通道。"""
    terms: list[str] = []
    seen: set[str] = set()
    for seg in _segments(question):
        if len(seg) == 1:
            continue  # 单字查询召回噪声太大，交给稠密通道
        for i in range(len(seg) - 1):
            term = seg[i : i + 2]
            if term in seen:
                continue
            if term[0] in _STOP_CHARS and term[1] in _STOP_CHARS:
                continue
            seen.add(term)
            terms.append(term)
            if len(terms) >= _MAX_TERMS:
                break
    if not terms:
        return None
    return " | ".join(terms)


async def _probe_cjk(db: AsyncSession) -> bool:
    """自检：`to_tsvector('simple', '陆沉 沉把')` 是否真的产出两个 lexeme。"""
    global _cjk_ok
    if _cjk_ok is not None:
        return _cjk_ok
    try:
        n = (
            await db.execute(
                # tsvector 不是数组，词素数用 length()（tsvector 专属重载），
                # 写成 array_length() 会直接报 "函数不存在"。
                text("SELECT length(to_tsvector('simple', '陆沉 沉把 把刀'))")
            )
        ).scalar()
        _cjk_ok = int(n or 0) >= 3
        if not _cjk_ok:
            logger.error(
                "PG 未能按空格切分中文 bigram（lexeme 数=%s），词法通道关闭，检索将走 dense_only", n
            )
    except Exception:  # noqa: BLE001
        _cjk_ok = False
        logger.exception("CJK 分词自检失败，词法通道关闭")
    return _cjk_ok


async def ensure_schema(db: AsyncSession) -> None:
    """表由 ORM 的 create_all 建好，这里只补索引（create_all 不建 GIN/表达式索引）。"""
    await db.execute(
        text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_tsv ON {TABLE} USING gin (tsv)")
    )
    await db.execute(
        text(f"CREATE INDEX IF NOT EXISTS ix_{TABLE}_novel ON {TABLE} (novel_id)")
    )
    await db.commit()


async def reindex_novel(novel_id: str, db: AsyncSession | None = None) -> int:
    """重建一本书的词法索引（先删后插，幂等）。"""
    own = db is None
    if own:
        db = SessionLocal()
    assert db is not None
    try:
        await ensure_schema(db)
        if not await _probe_cjk(db):
            return 0
        await db.execute(text(f"DELETE FROM {TABLE} WHERE novel_id = :nid"), {"nid": novel_id})
        await db.commit()

        rows = (
            await db.execute(
                text("SELECT id, content FROM chunks WHERE novel_id = :nid AND level = 1"),
                {"nid": novel_id},
            )
        ).all()

        stmt = text(
            f"INSERT INTO {TABLE}(chunk_id, novel_id, bigrams, tsv) "
            "VALUES (:cid, :nid, :bg, to_tsvector('simple', :bg)) "
            "ON CONFLICT (chunk_id) DO UPDATE SET "
            "novel_id = EXCLUDED.novel_id, bigrams = EXCLUDED.bigrams, tsv = EXCLUDED.tsv"
        )
        # 分批提交：2 万块一次性 executemany 要占几十 MB 内存，而且长时间握着写锁，
        # 会把正在跑的入库任务堵在 progress 更新上。2000 一批既省内存也让出锁。
        for start in range(0, len(rows), 2000):
            part = rows[start : start + 2000]
            await db.execute(
                stmt,
                [{"cid": r[0], "nid": novel_id, "bg": bigram_string(r[1])} for r in part],
            )
            await db.commit()
        logger.info("词法索引重建 novel=%s chunks=%d", novel_id, len(rows))
        return len(rows)
    finally:
        if own:
            await db.close()


async def remove_novel(novel_id: str, db: AsyncSession | None = None) -> None:
    """删书的词法索引。表上有 ON DELETE CASCADE，正常路径不需要调用，
    留给"清理 Milvus 时代遗留数据"这类手工操作。"""
    own = db is None
    if own:
        db = SessionLocal()
    assert db is not None
    try:
        await db.execute(text(f"DELETE FROM {TABLE} WHERE novel_id = :nid"), {"nid": novel_id})
        await db.commit()
    finally:
        if own:
            await db.close()


async def search(
    novel_id: str, question: str, limit: int = 50, db: AsyncSession | None = None
) -> list[tuple[str, float]]:
    """返回 [(chunk_id, score)]，score 越大越相关（ts_rank_cd 原值即此语义）。"""
    match = build_match_query(question)
    if not match:
        return []
    own = db is None
    if own:
        db = SessionLocal()
    assert db is not None
    try:
        if not await _probe_cjk(db):
            return []
        rows = (
            await db.execute(
                text(
                    f"SELECT chunk_id, ts_rank_cd(tsv, query) AS s "
                    f"FROM {TABLE}, to_tsquery('simple', :q) AS query "
                    "WHERE novel_id = :nid AND tsv @@ query "
                    "ORDER BY s DESC LIMIT :lim"
                ),
                {"q": match, "nid": novel_id, "lim": limit},
            )
        ).all()
        # 丢掉 0 分：ts_rank_cd 在"只命中停用词级高频 bigram"时会给 0，
        # 这批行彼此的相对顺序没有意义，喂进 RRF 就是噪声。
        return [(r[0], float(r[1])) for r in rows if float(r[1]) > 1e-9]
    except Exception:  # noqa: BLE001
        logger.warning("词法检索失败 novel=%s q=%r", novel_id, question[:40], exc_info=True)
        return []
    finally:
        if own:
            await db.close()


async def stats(db: AsyncSession | None = None) -> dict:
    """给 /system/stats 与 /health 用：索引覆盖率，方便发现"入库了但没建词法索引"。"""
    own = db is None
    if own:
        db = SessionLocal()
    assert db is not None
    try:
        indexed = (await db.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar() or 0
        total = (await db.execute(text("SELECT count(*) FROM chunks WHERE level = 1"))).scalar() or 0
        return {
            "indexed": int(indexed),
            "child_chunks": int(total),
            "ready": int(indexed) >= int(total),
            "engine": "postgres-tsvector",
        }
    finally:
        if own:
            await db.close()


async def ensure_fresh() -> int:
    """启动自检：索引行数和 child 块数对不上就整库重建。"""
    async with SessionLocal() as db:
        await ensure_schema(db)
        if not await _probe_cjk(db):
            return 0
        indexed = (await db.execute(text(f"SELECT count(*) FROM {TABLE}"))).scalar() or 0
        total = (await db.execute(text("SELECT count(*) FROM chunks WHERE level = 1"))).scalar() or 0
        if int(indexed) == int(total):
            return 0
        logger.warning("词法索引不一致（%d vs %d），全量重建", indexed, total)
    async with SessionLocal() as db:
        novel_ids = [r[0] for r in (await db.execute(text("SELECT DISTINCT novel_id FROM chunks"))).all()]
    n = 0
    for nid in novel_ids:
        n += await reindex_novel(nid)
    return n


__all__ = [
    "bigram_string",
    "build_match_query",
    "ensure_schema",
    "ensure_fresh",
    "reindex_novel",
    "remove_novel",
    "search",
    "stats",
]
