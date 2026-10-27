"""numpy 向量兜底库：pgvector 不可达时保证链路可跑。

把每个 novel 的向量落成 npz，检索用余弦暴力扫。数十万条以内可用，
它的存在意义是"PG 连不上或扩展没装时也能验收基础流程"，不是生产方案。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np

from app.config import BASE_DIR, settings
from app.services.vector.base import VectorHit


class NumpyVectorStore:
    name = "numpy"

    def __init__(self) -> None:
        self.dir = BASE_DIR / "data" / "vectors"
        self.dir.mkdir(parents=True, exist_ok=True)
        self._dim = settings.embedding_dim
        self._cache: dict[str, tuple[np.ndarray, list[dict]]] = {}

    def _paths(self, novel_id: str) -> tuple[Path, Path]:
        return self.dir / f"{novel_id}.npy", self.dir / f"{novel_id}.json"

    def _load(self, novel_id: str) -> tuple[np.ndarray, list[dict]]:
        if novel_id in self._cache:
            return self._cache[novel_id]
        vp, mp = self._paths(novel_id)
        if vp.exists() and mp.exists():
            vecs = np.load(vp)
            meta = json.loads(mp.read_text(encoding="utf-8"))
        else:
            vecs = np.zeros((0, self._dim), dtype=np.float32)
            meta = []
        self._cache[novel_id] = (vecs, meta)
        return vecs, meta

    async def ensure_ready(self, dim: int) -> dict:
        self._dim = dim
        return {"collection": f"numpy:{self.dir.name}", "created": True, "dim": dim}

    async def upsert(self, rows: list[dict]) -> int:
        def _do() -> int:
            by_novel: dict[str, list[dict]] = {}
            for r in rows:
                by_novel.setdefault(r["novel_id"], []).append(r)
            for novel_id, items in by_novel.items():
                old_v, old_m = self._load(novel_id)
                idx = {m["id"]: i for i, m in enumerate(old_m)}
                new_vecs = list(old_v) if len(old_v) else []
                new_meta = list(old_m)
                for it in items:
                    vec = np.asarray(it.pop("dense"), dtype=np.float32)
                    meta = {k: v for k, v in it.items() if k != "dense"}
                    if meta["id"] in idx:
                        pos = idx[meta["id"]]
                        new_vecs[pos] = vec
                        new_meta[pos] = meta
                    else:
                        new_vecs.append(vec)
                        new_meta.append(meta)
                arr = np.stack(new_vecs) if new_vecs else np.zeros((0, self._dim), dtype=np.float32)
                vp, mp = self._paths(novel_id)
                # 先写 json 再写 npy：崩溃时不会出现"向量有、元数据没有"的错位
                mp.write_text(json.dumps(new_meta, ensure_ascii=False), encoding="utf-8")
                np.save(vp, arr)
                self._cache.pop(novel_id, None)
            return len(rows)

        return await asyncio.to_thread(_do)

    async def delete_novel(self, novel_id: str) -> None:
        vp, mp = self._paths(novel_id)
        for p in (vp, mp):
            if p.exists():
                p.unlink()
        self._cache.pop(novel_id, None)

    async def existing_ids(self, novel_id: str) -> set[str]:
        _, meta = self._load(novel_id)
        return {str(m["id"]) for m in meta}

    async def search(self, novel_id: str, vector: list[float], top_k: int, level: int = 1) -> list[VectorHit]:
        def _do() -> list[VectorHit]:
            vecs, meta = self._load(novel_id)
            if not len(vecs):
                return []
            q = np.asarray(vector, dtype=np.float32)
            q = q / (np.linalg.norm(q) or 1.0)
            mat = vecs / np.maximum(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-9)
            scores = mat @ q
            order = np.argsort(-scores)
            hits: list[VectorHit] = []
            for i in order:
                m = meta[int(i)]
                if int(m.get("level", 1)) != level:
                    continue
                hits.append(
                    VectorHit(
                        id=m["id"],
                        score=float(scores[int(i)]),
                        novel_id=m["novel_id"],
                        level=int(m.get("level", 1)),
                        chapter_no=int(m.get("chapter_no", 0)),
                        seq=int(m.get("seq", 0)),
                        parent_id=m.get("parent_id", ""),
                        title=m.get("title", ""),
                        content=m.get("content", ""),
                        char_start=int(m.get("char_start", 0)),
                        char_end=int(m.get("char_end", 0)),
                    )
                )
                if len(hits) >= top_k:
                    break
            return hits

        return await asyncio.to_thread(_do)

    async def count(self, novel_id: str | None = None) -> int:
        def _do() -> int:
            if novel_id:
                return len(self._load(novel_id)[1])
            total = 0
            for mp in self.dir.glob("*.json"):
                total += len(json.loads(mp.read_text(encoding="utf-8")))
            return total

        return await asyncio.to_thread(_do)

    async def sample(self, novel_id: str, limit: int = 500) -> list[VectorHit]:
        _, meta = self._load(novel_id)
        return [
            VectorHit(
                id=m["id"],
                score=0.0,
                novel_id=m["novel_id"],
                level=int(m.get("level", 1)),
                chapter_no=int(m.get("chapter_no", 0)),
                seq=int(m.get("seq", 0)),
                parent_id=m.get("parent_id", ""),
                title=m.get("title", ""),
                content="",
                char_start=int(m.get("char_start", 0)),
                char_end=int(m.get("char_end", 0)),
            )
            for m in meta[:limit]
        ]

    async def health(self) -> dict:
        return {"name": "numpy", "status": "degraded", "detail": "本地兜底存储，非生产方案", "dim": self._dim}
