"""Embedding 工厂：按配置选择实现，auto 模式带降级。"""

from __future__ import annotations

import logging

from app.config import settings
from app.services.embedding.base import Embedder
from app.services.embedding.hash_embedder import HashEmbedder
from app.services.embedding.onnx_embedder import OnnxEmbedder
from app.services.embedding.tei_embedder import TEIEmbedder

logger = logging.getLogger(__name__)

_instance: Embedder | None = None


def get_embedder() -> Embedder:
    global _instance
    if _instance is not None:
        return _instance

    provider = settings.embedding_provider.lower()
    if provider == "hash":
        _instance = HashEmbedder()
    elif provider == "tei":
        _instance = TEIEmbedder()
    else:  # auto | onnx：本地优先，加载失败降级到 hash，绝不阻塞主链路
        try:
            emb = OnnxEmbedder()
            emb._load()
            emb._ready = True
            _instance = emb
        except Exception as exc:  # noqa: BLE001
            logger.warning("本地 ONNX 模型不可用，降级哈希向量：%s", exc)
            _instance = HashEmbedder()
    logger.info("embedder=%s dim=%s", _instance.name, getattr(_instance, "dim", "?"))
    return _instance


def reset_embedder() -> None:
    global _instance
    _instance = None
