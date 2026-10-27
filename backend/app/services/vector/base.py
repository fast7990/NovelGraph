"""向量库抽象。Milvus 是主实现，numpy 是断网兜底。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass
class VectorHit:
    id: str
    score: float
    novel_id: str
    level: int
    chapter_no: int
    seq: int
    parent_id: str
    title: str
    content: str
    char_start: int
    char_end: int


class VectorStore(Protocol):
    name: str

    async def ensure_ready(self, dim: int) -> dict: ...

    async def upsert(self, rows: list[dict]) -> int: ...

    async def delete_novel(self, novel_id: str) -> None: ...

    async def search(self, novel_id: str, vector: list[float], top_k: int, level: int = 1) -> list[VectorHit]: ...

    async def count(self, novel_id: str | None = None) -> int: ...

    async def sample(self, novel_id: str, limit: int = 500) -> list[VectorHit]: ...

    async def health(self) -> dict: ...
