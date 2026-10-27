"""零依赖兜底 Embedder：字符 n-gram 哈希投影。

用途：模型下载失败 / 断网 / 单测环境，让整条链路仍然能跑通，
只是召回质量明显下降（`degraded=true` 会在健康检查里暴露）。

算法：中文按字 bigram、英文按词，哈希到固定维度后 L2 归一化。
这不是"语义向量"，但在同一本书内部做关键词近似匹配够用。
"""

from __future__ import annotations

import hashlib
import math
import re

from app.config import settings
from app.services.embedding.base import BaseEmbedder

WORD = re.compile(r"[a-zA-Z0-9]+")


class HashEmbedder(BaseEmbedder):
    name = "hash"
    degraded = True

    def __init__(self, dim: int | None = None) -> None:
        super().__init__()
        self.dim = dim or settings.embedding_dim
        self._ready = True

    def _load(self) -> None:  # 无需加载
        return

    def _tokens(self, text: str) -> list[str]:
        toks: list[str] = [w.lower() for w in WORD.findall(text)]
        chars = [c for c in text if "\u4e00" <= c <= "\u9fff"]
        toks.extend(chars)
        toks.extend(chars[i] + chars[i + 1] for i in range(len(chars) - 1))
        return toks

    def _encode(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for text in texts:
            vec = [0.0] * self.dim
            for tok in self._tokens(text):
                h = hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest()
                idx = int.from_bytes(h[:4], "little") % self.dim
                sign = 1.0 if h[4] & 1 else -1.0
                vec[idx] += sign
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    async def health(self) -> dict:
        return {
            "name": self.name,
            "dim": self.dim,
            "degraded": True,
            "status": "degraded",
            "note": "哈希兜底向量，仅保证链路可用，无语义能力",
        }
