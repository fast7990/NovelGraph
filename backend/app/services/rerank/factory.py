"""精排器工厂（单例）。

只做一件事：保证全进程只有一个 ONNX session。
每次请求新建 session 会让内存翻倍、首 token 延迟抖动到秒级。
"""

from __future__ import annotations

import logging

from app.config import settings
from app.services.rerank.onnx_reranker import OnnxReranker

logger = logging.getLogger(__name__)

_reranker: OnnxReranker | None = None


def get_reranker() -> OnnxReranker:
    global _reranker
    if _reranker is None:
        _reranker = OnnxReranker()
        if not settings.rerank_enabled:
            _reranker.error = "RERANK_ENABLED=false"
    return _reranker


async def warmup() -> None:
    r = get_reranker()
    if not settings.rerank_enabled:
        logger.info("精排已关闭（RERANK_ENABLED=false），检索走 RRF 排序")
        return
    await r.warmup()
