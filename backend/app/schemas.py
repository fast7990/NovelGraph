"""响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UploadResponse(BaseModel):
    novel_id: str
    job_id: str
    filename: str = ""
    file_size: int = 0
    message: str = "已入队，正在解析"


class NovelOut(BaseModel):
    id: str
    title: str
    filename: str
    file_size: int
    encoding: str
    char_count: int
    chapter_count: int
    chunk_count: int
    status: str
    created_at: datetime | None = None


class NovelListOut(BaseModel):
    items: list[NovelOut]
    total: int


class JobOut(BaseModel):
    id: str
    novel_id: str
    status: str
    stage: str
    progress: float
    chars_done: int
    chars_total: int
    chapters_done: int
    chapters_total: int
    chunks_created: int
    vectors_written: int
    error: str = ""
    stats: dict = Field(default_factory=dict)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ChunkOut(BaseModel):
    id: str
    parent_id: str | None
    level: int
    chapter_no: int
    seq: int
    title: str
    content: str
    char_start: int
    char_end: int
    token_est: int


class ChunkPage(BaseModel):
    items: list[ChunkOut]
    total: int
    page: int
    page_size: int


class ReportItem(BaseModel):
    chapter_no: int
    code: str
    severity: str
    detail: str
    char_start: int


class ChunkReportOut(BaseModel):
    novel_id: str
    chapter_count: int = 0
    chunk_count: int = 0
    parent_count: int = 0
    char_count: int = 0
    covered_chars: int = 0
    completeness: float = 0.0
    avg_tokens: float = 0.0
    p95_tokens: int = 0
    token_hist: list[int] = Field(default_factory=list)
    token_buckets: list[int] = Field(default_factory=list)
    issues: list[ReportItem] = Field(default_factory=list)
    issues_by_code: dict[str, int] = Field(default_factory=dict)
    issues_by_severity: dict[str, int] = Field(default_factory=dict)
    sample_chunks: list[ChunkOut] = Field(default_factory=list)


class ChatRequest(BaseModel):
    novel_id: str
    question: str
    session_id: str | None = None
    top_k: int | None = None


class SearchRequest(BaseModel):
    novel_id: str
    query: str
    top_k: int = 10


class HealthItem(BaseModel):
    name: str
    status: str
    detail: str | None = None
