"""ORM 模型。三份存储的分工：

- SQLite/PostgreSQL：元数据 + 切片正文（唯一事实来源）
- Milvus：只存可检索单元（child / 章摘要）的向量
- Neo4j：P1 才接入，P0 留空
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base


def _now() -> datetime:
    return datetime.now(timezone.utc)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


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
    meta: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    updated_at: Mapped[datetime] = mapped_column(default=_now, onupdate=_now)


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
    stats: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=_now)
    started_at: Mapped[datetime | None] = mapped_column(default=None)
    finished_at: Mapped[datetime | None] = mapped_column(default=None)


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
    created_at: Mapped[datetime] = mapped_column(default=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cm"))
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    role: Mapped[str] = mapped_column(String(20), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(default=_now)
