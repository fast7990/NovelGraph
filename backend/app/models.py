"""ORM 模型。P1 起所有数据都在 PostgreSQL：

- `novels / chapters / chunks / chunk_reports / jobs / chat_*`：元数据与切片正文
  （唯一事实来源，检索结果最终都回到 `chunks` 取原文）
- `chunk_vectors`：可检索单元（child / 章摘要）的向量，pgvector 存储
- `chunk_lexical`：同一批单元的词法倒排（bigram → tsvector）
- Neo4j：P1 图谱阶段才接入，先用 GraphStore 抽象挡着

`chunk_vectors` 与 `chunk_lexical` 都对 `chunks(id)` 加了 ON DELETE CASCADE：
删书时 PG 会自己收拾干净，不再需要应用层"记得删向量"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class _TZ:
    """所有时间列统一 timestamptz。

    SQLite 时代没有时区概念，迁到 PG 后如果还写成 TIMESTAMP WITHOUT TIME ZONE，
    asyncpg 会拒绝写入带 tzinfo 的 datetime（而 `_now()` 返回的就是 aware），
    第一次跑入库就会炸在 commit 上。统一带时区，省掉一整类问题。
    """

    @staticmethod
    def col(**kwargs):  # noqa: ANN003
        return mapped_column(DateTime(timezone=True), **kwargs)


class Novel(Base):
    __tablename__ = "novels"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("nv"))
    title: Mapped[str] = mapped_column(String(200), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    storage_key: Mapped[str] = mapped_column(String(512), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    encoding: Mapped[str] = mapped_column(String(32), default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _TZ.col(default=_now)
    updated_at: Mapped[datetime] = _TZ.col(default=_now, onupdate=_now)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING/RUNNING/COMPLETED/FAILED/CANCELED
    stage: Mapped[str] = mapped_column(String(20), default="PENDING")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    # 切片完成度以字符为准：块数在流式切分中事前未知，用块数会出现"越跑越退"
    chars_done: Mapped[int] = mapped_column(Integer, default=0)
    chars_total: Mapped[int] = mapped_column(Integer, default=0)
    chunks_created: Mapped[int] = mapped_column(Integer, default=0)
    chapters_done: Mapped[int] = mapped_column(Integer, default=0)
    chapters_total: Mapped[int] = mapped_column(Integer, default=0)
    vectors_written: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _TZ.col(default=_now)
    started_at: Mapped[datetime | None] = _TZ.col(default=None)
    finished_at: Mapped[datetime | None] = _TZ.col(default=None)


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ch"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200), default="")
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (Index("ix_chapters_novel_no", "novel_id", "chapter_no"),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ck"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)  # 0=parent 1=child 3=章摘要
    chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    token_est: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_chunks_novel_level", "novel_id", "level"),
        Index("ix_chunks_novel_chapter", "novel_id", "chapter_no", "seq"),
    )


class ChunkVector(Base):
    """向量单独成表，而不是塞进 `chunks`。

    理由：入库是"先写正文、再逐批补向量"的两段式，如果把 4KB 的向量放进 chunks 行里，
    每批 update 都会重写整行并触发 TOAST 检查，2 万块的书会攒下大量死元组；
    分开后 chunks 保持只读，向量表只管自己的插入与 HNSW 维护。
    """

    __tablename__ = "chunk_vectors"

    chunk_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("chunks.id", ondelete="CASCADE", name="fk_chunk_vectors_chunk"),
        primary_key=True,
    )
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    # 维度与 embedding 模型绑定：换模型（512 ↔ 1024）必须重建这张表，
    # 建表时把维度写进列类型，插错维度 PG 会直接拒绝，比应用层校验更可靠。
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=False)


class ChunkLexical(Base):
    """词法倒排：一个 child 块一行。

    `tsv` 存的是**bigram 切分后**的 tsvector（见 services/retrieval/lexical.py），
    不是原文直接 to_tsvector —— PG 默认分词器对中文整段当一个 token，
    不做切分的话"陆沉"这种两字词根本进不了倒排表。
    """

    __tablename__ = "chunk_lexical"

    chunk_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("chunks.id", ondelete="CASCADE", name="fk_chunk_lexical_chunk"),
        primary_key=True,
    )
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    # 保留切分后的原始串：排查"某个词为什么搜不到"时可以直接看一眼切成了什么，
    # 也方便以后换分词方案时不用回表重算
    bigrams: Mapped[str] = mapped_column(Text, default="")
    tsv: Mapped[str] = mapped_column(TSVECTOR, nullable=False)


class ChunkReport(Base):
    """切片质量告警。前端"完成度"页的第二块内容。"""

    __tablename__ = "chunk_reports"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rp"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    job_id: Mapped[str] = mapped_column(String(40), index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    code: Mapped[str] = mapped_column(String(40), default="")
    severity: Mapped[str] = mapped_column(String(10), default="warn")  # info/warn/error
    detail: Mapped[str] = mapped_column(Text, default="")
    char_start: Mapped[int] = mapped_column(Integer, default=0)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cs"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[datetime] = _TZ.col(default=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cm"))
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    role: Mapped[str] = mapped_column(String(20), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = _TZ.col(default=_now)
