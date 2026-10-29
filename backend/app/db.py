"""数据库连接与会话（PostgreSQL + pgvector）。

P1 起元数据、切片、向量、词法索引**全在一个 PG 实例**里，原因：

1. 向量不再需要单独一套服务，写完立刻可查，没有"双写不一致"的窗口；
2. `chunk_vectors.chunk_id` 用外键 + ON DELETE CASCADE 指向 `chunks`，
   删书时向量自动跟着走 —— 之前 Milvus 时代出现过"书删了、向量还在跑"的孤儿问题，
   根子上就是两个库没有约束关系；
3. 入库和检索共用一个连接池，少一套连接管理。

代价是向量和业务表抢同一个实例的 IO，超大语料（千万级）时会成为瓶颈，
那时再把 `chunk_vectors` 拆到独立实例即可（表结构不用动，改 DSN 就行）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(
    settings.database_url,
    echo=False,
    future=True,
    pool_pre_ping=True,
    pool_size=8,
    max_overflow=8,
    # 并发入库时语句缓存会反复失效，关掉反而更稳（asyncpg 的 statement_cache 与 DDL 冲突）
    connect_args={"server_settings": {"application_name": "novel-rag", "timezone": "UTC"}},
)

SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


REQUIRED_EXTENSIONS = ("vector",)

_MISSING_EXT_HINT = """\
pgvector 扩展未安装。两种装法任选：

  1) 预编译包（PostgreSQL 18 / Windows，推荐）
     https://github.com/andreiramani/pgvector_pgsql_windows/releases
     下载 vector.v0.8.7-pg18.zip，解压覆盖到 PostgreSQL 安装目录
     （D:\\Program Files\\PostgreSQL\\18），再执行下面的 SQL。

  2) 源码编译：set PGROOT=<PG安装目录> && nmake /F Makefile.win && nmake /F Makefile.win install

装完后用**超级用户**在业务库里启用一次：

  psql -U postgres -d novel_rag -c "CREATE EXTENSION IF NOT EXISTS vector;"

扩展文件是装在 PostgreSQL 安装目录下的，跟具体数据库无关；
但 CREATE EXTENSION 是按库生效的，每个新建的库都要执行一次。
"""


async def init_db() -> None:
    from app import models  # noqa: F401  确保模型已注册

    async with engine.begin() as conn:
        for ext in REQUIRED_EXTENSIONS:
            installed = (
                await conn.execute(
                    text("SELECT 1 FROM pg_extension WHERE extname = :n"), {"n": ext}
                )
            ).scalar()
            if installed:
                continue
            try:
                await conn.execute(text(f"CREATE EXTENSION IF NOT EXISTS {ext}"))
                logger.info("已启用扩展 %s", ext)
            except Exception as exc:  # noqa: BLE001
                raise RuntimeError(f"{_MISSING_EXT_HINT}\n底层错误：{exc}") from exc

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _apply_patches(conn)


# 生成式建表不会给**已存在**的表补列，而项目还没上 alembic。
# 这里用幂等 DDL 兜底：新装的环境 create_all 已建好，老库执行一次 ALTER 就对齐。
# 上 alembic 之后删掉本段，改为版本化迁移。
_COLUMN_PATCHES = (
    # P1-M5：任务分种类，"图谱构建"和"入库"共用一个表但队列处理方式不同
    "ALTER TABLE jobs ADD COLUMN IF NOT EXISTS kind VARCHAR(20) NOT NULL DEFAULT 'ingest'",
    # 会话记忆：会话按最近活跃排序、消息按会话内轮次排序
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS updated_at TIMESTAMPTZ NOT NULL DEFAULT now()",
    "ALTER TABLE chat_messages ADD COLUMN IF NOT EXISTS seq INTEGER NOT NULL DEFAULT 0",
    # 长对话的滚动摘要 + 问答 token 台账
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS summary TEXT NOT NULL DEFAULT ''",
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS summary_seq INTEGER NOT NULL DEFAULT 0",
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS prompt_tokens BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS completion_tokens BIGINT NOT NULL DEFAULT 0",
    "ALTER TABLE chat_sessions ADD COLUMN IF NOT EXISTS llm_calls INTEGER NOT NULL DEFAULT 0",
)


async def _apply_patches(conn) -> None:  # noqa: ANN001
    for sql in _COLUMN_PATCHES:
        await conn.execute(text(sql))
