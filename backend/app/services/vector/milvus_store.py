"""Milvus 3.0（Zilliz Serverless）适配器。

设计取舍
- P0 只建稠密索引；稀疏（BM25）在 P1 打开，schema 已预留 `sparse` 字段位。
- collection 名带维度后缀：换 embedding 模型（512 ↔ 1024）时**必须**是新 collection，
  否则 insert 会直接报维度不匹配。用命名区分比让运维记住"改配置要删库"更可靠。
- 所有同步 SDK 调用都包 asyncio.to_thread：pymilvus 是阻塞式 HTTP/gRPC，
  直接在事件循环里跑会卡住 SSE 推送。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.config import settings
from app.services.vector.base import VectorHit

logger = logging.getLogger(__name__)

CONTENT_MAX = 16000


class MilvusVectorStore:
    name = "milvus"

    def __init__(self) -> None:
        self.uri = settings.milvus_uri
        self.token = settings.milvus_token
        self.collection = settings.milvus_collection
        self._client: Any = None
        self._dim = settings.milvus_dim
        self._lock = asyncio.Lock()
        self._ready = False

    # ---------- 连接 ----------
    def _connect_sync(self) -> Any:
        from pymilvus import MilvusClient

        return MilvusClient(
            uri=self.uri,
            token=self.token,
            db_name=settings.milvus_db_name,
            timeout=settings.milvus_timeout,
        )

    async def _client_or_connect(self) -> Any:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                self._client = await asyncio.to_thread(self._connect_sync)
        return self._client

    def collection_name(self, dim: int | None = None) -> str:
        dim = dim or self._dim
        if dim == settings.milvus_dim:
            return self.collection
        return f"{self.collection}_d{dim}"

    # ---------- schema ----------
    def _ensure_sync(self, dim: int) -> dict:
        from pymilvus import DataType

        client = self._connect_sync()
        self._client = client
        self._dim = dim
        name = self.collection_name(dim)

        if client.has_collection(name):
            desc = client.describe_collection(name)
            for f in desc.get("fields", []):
                if f.get("name") == "dense":
                    has_dim = f.get("params", {}).get("dim")
                    if has_dim and int(has_dim) != dim:
                        raise RuntimeError(
                            f"collection {name} 向量维度为 {has_dim}，当前 Embedding 输出 {dim}。"
                            f"请修改 MILVUS_COLLECTION 或清空该集合后重建。"
                        )
            try:
                client.load_collection(name)
            except Exception:  # noqa: BLE001  已加载会抛错，忽略
                pass
            return {"collection": name, "created": False, "dim": dim}

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("novel_id", DataType.VARCHAR, max_length=64)
        schema.add_field("level", DataType.INT8)
        schema.add_field("chapter_no", DataType.INT32)
        schema.add_field("seq", DataType.INT32)
        schema.add_field("parent_id", DataType.VARCHAR, max_length=64)
        schema.add_field("title", DataType.VARCHAR, max_length=255)
        schema.add_field("content", DataType.VARCHAR, max_length=CONTENT_MAX)
        schema.add_field("char_start", DataType.INT64)
        schema.add_field("char_end", DataType.INT64)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dim)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense",
            index_type=settings.milvus_index_type,
            metric_type=settings.milvus_metric,
            params={},
        )
        client.create_collection(name, schema=schema, index_params=index_params)
        client.load_collection(name)
        logger.info("created milvus collection %s dim=%s", name, dim)
        return {"collection": name, "created": True, "dim": dim}

    async def ensure_ready(self, dim: int) -> dict:
        if self._ready and self._dim == dim:
            return {"collection": self.collection_name(dim), "ready": True}
        info = await asyncio.to_thread(self._ensure_sync, dim)
        self._ready = True
        return info

    # ---------- 写入 ----------
    async def upsert(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        client = await self._client_or_connect()
        name = self.collection_name()

        def _do() -> int:
            client.upsert(collection_name=name, data=rows)
            return len(rows)

        return await asyncio.to_thread(_do)

    async def delete_novel(self, novel_id: str) -> None:
        client = await self._client_or_connect()
        name = self.collection_name()
        await asyncio.to_thread(
            lambda: client.delete(collection_name=name, filter=f'novel_id == "{novel_id}"')
        )

    # ---------- 查询 ----------
    async def search(self, novel_id: str, vector: list[float], top_k: int, level: int = 1) -> list[VectorHit]:
        client = await self._client_or_connect()
        name = self.collection_name()
        expr = f'novel_id == "{novel_id}" and level == {level}'

        def _do() -> list[VectorHit]:
            res = client.search(
                collection_name=name,
                data=[vector],
                filter=expr,
                limit=top_k,
                output_fields=[
                    "novel_id",
                    "level",
                    "chapter_no",
                    "seq",
                    "parent_id",
                    "title",
                    "content",
                    "char_start",
                    "char_end",
                ],
                search_params={"metric_type": settings.milvus_metric},
                consistency_level=settings.milvus_consistency,
            )
            hits: list[VectorHit] = []
            for row in (res[0] if res else []):
                ent = row.get("entity") or {}
                hits.append(
                    VectorHit(
                        id=str(row.get("id")),
                        score=float(row.get("distance", 0.0)),
                        novel_id=ent.get("novel_id", ""),
                        level=int(ent.get("level", 1)),
                        chapter_no=int(ent.get("chapter_no", 0)),
                        seq=int(ent.get("seq", 0)),
                        parent_id=ent.get("parent_id", "") or "",
                        title=ent.get("title", ""),
                        content=ent.get("content", ""),
                        char_start=int(ent.get("char_start", 0)),
                        char_end=int(ent.get("char_end", 0)),
                    )
                )
            return hits

        return await asyncio.to_thread(_do)

    async def count(self, novel_id: str | None = None) -> int:
        client = await self._client_or_connect()
        name = self.collection_name()
        expr = f'novel_id == "{novel_id}"' if novel_id else ""

        def _do() -> int:
            # 强一致：刚写完立刻 count，Bounded 会读到旧快照（实测 144 读成 128）
            res = client.query(
                collection_name=name,
                filter=expr,
                output_fields=["count(*)"],
                consistency_level=settings.milvus_consistency,
            )
            return int(res[0]["count(*)"]) if res else 0

        return await asyncio.to_thread(_do)

    async def sample(self, novel_id: str, limit: int = 500) -> list[VectorHit]:
        client = await self._client_or_connect()
        name = self.collection_name()

        def _do() -> list[VectorHit]:
            res = client.query(
                collection_name=name,
                filter=f'novel_id == "{novel_id}"',
                output_fields=["id", "novel_id", "level", "chapter_no", "seq", "parent_id", "title", "char_start", "char_end"],
                limit=limit,
            )
            return [
                VectorHit(
                    id=str(r.get("id")),
                    score=0.0,
                    novel_id=r.get("novel_id", ""),
                    level=int(r.get("level", 1)),
                    chapter_no=int(r.get("chapter_no", 0)),
                    seq=int(r.get("seq", 0)),
                    parent_id=r.get("parent_id", "") or "",
                    title=r.get("title", ""),
                    content="",
                    char_start=int(r.get("char_start", 0)),
                    char_end=int(r.get("char_end", 0)),
                )
                for r in res
            ]

        return await asyncio.to_thread(_do)

    async def health(self) -> dict:
        err = settings.milvus_config_error()
        if err:
            return {"name": "milvus", "status": "down", "detail": err}
        try:
            client = await self._client_or_connect()
            version = await asyncio.to_thread(lambda: client.get_server_version())
            return {
                "name": "milvus",
                "status": "ok",
                "server_version": version,
                "collection": self.collection_name(),
                "dim": self._dim,
            }
        except Exception as exc:  # noqa: BLE001
            return {"name": "milvus", "status": "down", "detail": str(exc)}
