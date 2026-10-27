"""TEI（text-embeddings-inference）HTTP 客户端。

留着是因为上 GPU 后把 TEI 跑成独立容器、多副本是最省事的扩展方式，
业务代码一行不用改，只把 EMBEDDING_PROVIDER 改成 tei。
"""

from __future__ import annotations

import httpx

from app.config import settings
from app.services.embedding.base import BaseEmbedder


class TEIEmbedder(BaseEmbedder):
    name = "tei"

    def __init__(self, base_url: str | None = None) -> None:
        super().__init__()
        self.base_url = (base_url or "http://127.0.0.1:8080").rstrip("/")
        self.dim = settings.embedding_dim
        self._client: httpx.Client | None = None

    def _load(self) -> None:
        self._client = httpx.Client(timeout=60.0)
        resp = self._client.post(f"{self.base_url}/embed", json={"inputs": ["维度探测"]})
        resp.raise_for_status()
        self.dim = len(resp.json()[0])

    def _encode(self, texts: list[str]) -> list[list[float]]:
        if self._client is None:
            raise RuntimeError("TEI 客户端未初始化")
        out: list[list[float]] = []
        bs = settings.embedding_batch
        for i in range(0, len(texts), bs):
            resp = self._client.post(
                f"{self.base_url}/embed",
                json={"inputs": texts[i : i + bs], "normalize": True},
            )
            resp.raise_for_status()
            out.extend(resp.json())
        return out

    async def health(self) -> dict:
        try:
            async with httpx.AsyncClient(timeout=5.0) as c:
                r = await c.get(f"{self.base_url}/health")
            return {"name": self.name, "dim": self.dim, "status": "ok" if r.status_code == 200 else "down"}
        except Exception as exc:  # noqa: BLE001
            return {"name": self.name, "dim": self.dim, "status": "down", "error": str(exc)}
