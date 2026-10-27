"""ONNX 本地向量化（bge-small-zh-v1.5）。

为什么自己写而不用 fastembed / sentence-transformers：
- fastembed 走 huggingface_hub，国内拉 LFS 会卡在 Xet CAS（401），无法在生产复现；
- sentence-transformers 要拖 torch（2.5G），4 核 CPU 场景纯属浪费。
自己写只有 3 个依赖：tokenizers + onnxruntime + numpy，模型文件支持从 ModelScope 拉，
国内网络可用，行为完全可控。

池化方式：BGE 系列用 **CLS**（首 token），不是 mean pooling —— 用错会明显掉分。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import numpy as np

from app.config import settings
from app.services.embedding.base import BaseEmbedder

logger = logging.getLogger(__name__)

MODEL_DIR_NAME = "bge-small-zh-v1.5"
REQUIRED_FILES = [
    "model_optimized.onnx",
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
]
# ModelScope 源（国内可达）；HF 源留作备用
MS_REPO = "Qdrant/bge-small-zh-v1.5"
MS_URL = "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={file}"

BGE_ZH_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


class OnnxEmbedder(BaseEmbedder):
    name = "onnx"

    def __init__(self, model_dir: Path | None = None, dim: int | None = None) -> None:
        super().__init__()
        self.model_dir = Path(model_dir or (settings.cache_dir / MODEL_DIR_NAME))
        self.dim = dim or settings.embedding_dim
        self._session = None
        self._tokenizer = None
        self._input_names: set[str] = set()

    # ---------- 模型文件 ----------
    def _missing(self) -> list[str]:
        return [f for f in REQUIRED_FILES if not (self.model_dir / f).exists()]

    def _download_missing(self) -> None:
        """缺失文件从 ModelScope 补齐（94MB 主模型大约 1-3 分钟，取决于带宽）。"""
        missing = self._missing()
        if not missing:
            return
        self.model_dir.mkdir(parents=True, exist_ok=True)
        logger.info("下载模型文件: %s", missing)
        with httpx.Client(timeout=600, follow_redirects=True) as client:
            for f in missing:
                url = MS_URL.format(repo=MS_REPO, file=f)
                tmp = self.model_dir / (f + ".part")
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    total = int(resp.headers.get("content-length", 0))
                    got = 0
                    with tmp.open("wb") as fh:
                        for chunk in resp.iter_bytes(1 << 20):
                            fh.write(chunk)
                            got += len(chunk)
                if total and got < total * 0.95:
                    raise RuntimeError(f"{f} 下载不完整（{got}/{total}）")
                tmp.replace(self.model_dir / f)
                logger.info("下载完成 %s (%.1f MB)", f, got / 1e6)

    # ---------- 加载 ----------
    def _load(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self._download_missing()

        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, settings.embedding_threads)
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(self.model_dir / "model_optimized.onnx"),
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=settings.embedding_max_tokens)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

        probe = self._encode(["维度探测"])
        self.dim = len(probe[0])
        logger.info("onnx embedder ready dim=%s threads=%s path=%s", self.dim, settings.embedding_threads, self.model_dir)

    # ---------- 推理 ----------
    def _encode(self, texts: list[str]) -> list[list[float]]:
        if self._session is None or self._tokenizer is None:
            raise RuntimeError("embedder 未加载")
        encs = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)
        hidden = self._session.run(None, feeds)[0]
        # CLS pooling + L2 归一化（归一化后才能直接用 COSINE/IP）
        vecs = hidden[:, 0, :]
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        vecs = vecs / np.maximum(norms, 1e-9)
        return vecs.astype(np.float32).tolist()

    def _query_prefix(self) -> str:
        return BGE_ZH_QUERY_PREFIX

    async def health(self) -> dict:
        return {
            "name": self.name,
            "model": MODEL_DIR_NAME,
            "dim": self.dim,
            "threads": settings.embedding_threads,
            "degraded": False,
            "status": "ok" if self._ready else "loading",
            "path": str(self.model_dir),
        }
