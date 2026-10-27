"""向量库工厂。

P1 起默认 pgvector（向量和正文同库，少一套服务、少一类不一致）。
保留 numpy 兜底：断网或扩展装不上时仍然能把 P0 链路跑通，
此时检索退化为暴力扫描，`/health` 会明确标出来。
"""

from __future__ import annotations

import logging

from app.config import settings
from app.services.vector.base import VectorStore

logger = logging.getLogger(__name__)

_store: VectorStore | None = None


def backend_preference() -> str:
    return (settings.vector_backend or "pgvector").lower()


async def get_vector_store() -> VectorStore:
    global _store
    if _store is not None:
        return _store

    pref = backend_preference()
    if pref == "numpy":
        _store = _NumpyStoreProxy()
        return _store

    from app.services.vector.pgvector_store import PgVectorStore

    store = PgVectorStore()
    if pref == "pgvector":
        _store = store
        return _store

    # auto：先试 pgvector，起不来再退 numpy
    try:
        from app.services.embedding.factory import get_embedder

        await store.ensure_ready(get_embedder().dim)
        _store = store
        logger.info("vector store = pgvector (%s)", settings.pgvector_table)
    except Exception as exc:  # noqa: BLE001
        logger.warning("pgvector 不可用，降级 numpy 暴力扫描：%s", exc)
        _store = _NumpyStoreProxy()
    return _store


def reset_store() -> None:
    """测试与运维用：强制下次重新构造（改完配置不必重启进程）。"""
    global _store
    _store = None


class _NumpyStoreProxy:
    """延迟构造，避免在 import 期建目录。"""

    name = "numpy"

    def __init__(self) -> None:
        from app.services.vector.numpy_store import NumpyVectorStore

        self._impl = NumpyVectorStore()

    def __getattr__(self, item):  # noqa: ANN001
        return getattr(self._impl, item)
