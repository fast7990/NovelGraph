"""向量库工厂。

VECTOR_BACKEND 未显式指定时：先试 Milvus（连接 + 探测 collection），
失败则退到 numpy，并在 /health 里标 degraded —— 宁可降级可用，
也不要因为"库连不上"让整个 P0 验收卡住。
"""

from __future__ import annotations

import logging
import os

from app.config import settings
from app.services.vector.base import VectorStore

logger = logging.getLogger(__name__)

_store: VectorStore | None = None


def backend_preference() -> str:
    return os.environ.get("VECTOR_BACKEND", "auto").lower()


async def get_vector_store() -> VectorStore:
    global _store
    if _store is not None:
        return _store

    pref = backend_preference()
    if pref == "numpy":
        _store = _NumpyStoreProxy()
        return _store

    from app.services.vector.milvus_store import MilvusVectorStore

    milvus = MilvusVectorStore()
    if pref == "milvus":
        _store = milvus
        return _store

    cfg_err = settings.milvus_config_error()
    if cfg_err:
        logger.warning("Milvus 配置异常，降级 numpy：%s", cfg_err)
        _store = _NumpyStoreProxy()
        return _store

    try:
        from app.services.embedding.factory import get_embedder

        dim = get_embedder().dim
        await milvus.ensure_ready(dim)
        _store = milvus
        logger.info("vector store = milvus (%s)", milvus.collection_name())
    except Exception as exc:  # noqa: BLE001
        logger.warning("Milvus 不可用，降级 numpy：%s", exc)
        _store = _NumpyStoreProxy()
    return _store


class _NumpyStoreProxy:
    """延迟构造，避免在 import 期建目录。"""

    name = "numpy"

    def __init__(self) -> None:
        from app.services.vector.numpy_store import NumpyVectorStore

        self._impl = NumpyVectorStore()

    def __getattr__(self, item):  # noqa: ANN001
        return getattr(self._impl, item)
