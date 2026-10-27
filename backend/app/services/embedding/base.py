"""Embedding 抽象。

上层只依赖 `Embedder` 协议，换本地 ONNX / TEI / 云端 API 不改调用方。
真正的向量化是 CPU 密集操作，必须丢到线程池，不能阻塞事件循环。
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable


@runtime_checkable
class Embedder(Protocol):
    name: str
    dim: int
    degraded: bool

    async def embed_documents(self, texts: list[str]) -> list[list[float]]: ...

    async def embed_query(self, text: str) -> list[float]: ...

    async def health(self) -> dict: ...


@dataclass
class EmbedderInfo:
    name: str
    dim: int
    degraded: bool
    note: str = ""


class BaseEmbedder:
    name = "base"
    dim = 512
    degraded = False

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._ready = False

    async def _ensure(self) -> None:
        if self._ready:
            return
        async with self._lock:
            if not self._ready:
                await asyncio.to_thread(self._load)
                self._ready = True

    def _load(self) -> None:  # pragma: no cover - 子类实现
        raise NotImplementedError

    def _encode(self, texts: list[str]) -> list[list[float]]:  # pragma: no cover
        raise NotImplementedError

    async def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        await self._ensure()
        return await asyncio.to_thread(self._encode, texts)

    async def embed_query(self, text: str) -> list[float]:
        vecs = await self.embed_documents([self._query_prefix() + text])
        return vecs[0]

    def _query_prefix(self) -> str:
        return ""

    async def health(self) -> dict:
        return {"name": self.name, "dim": self.dim, "degraded": self.degraded, "status": "ok"}
