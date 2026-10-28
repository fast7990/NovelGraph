"""ONNX 本地向量化（默认 bge-large-zh-v1.5，1024 维）。

为什么自己写而不用 fastembed / sentence-transformers：
- fastembed 走 huggingface_hub，国内拉 LFS 会卡在 Xet CAS（401），无法在生产复现；
- sentence-transformers 要拖 torch（2.5G），纯 CPU 场景纯属浪费。
自己写只有 3 个依赖：tokenizers + onnxruntime + numpy，模型文件从 ModelScope 拉，
国内网络可用，行为完全可控。

池化方式：BGE 系列用 **CLS**（首 token），不是 mean pooling —— 用错会明显掉分。
注意 pooler_output 也不能用：它是 tanh(dense(CLS))，BGE 训练时没用这一层。

量化版说明：默认拉 Xenova 导出的 int8 动态量化权重（326MB / 1024 维），
相比 fp32（1.3GB）体积 1/4、CPU 上快 2-3 倍，检索指标损失在 1% 量级。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
import numpy as np

from app.config import settings
from app.services.embedding.base import BaseEmbedder
from app.services.embedding.errors import FatalModelError

logger = logging.getLogger(__name__)

# 远程路径可能带子目录（onnx/model_quantized.onnx），落盘统一拍平成本地名
LOCAL_ONNX = "model.onnx"
SIDECAR_FILES = [
    "config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "vocab.txt",
]

MS_URL = "https://www.modelscope.cn/api/v1/models/{repo}/repo?Revision=master&FilePath={file}"

BGE_ZH_QUERY_PREFIX = "为这个句子生成表示以用于检索相关文章："


def model_dir_name(model_id: str) -> str:
    """BAAI/bge-large-zh-v1.5 -> bge-large-zh-v1.5"""
    return model_id.rstrip("/").split("/")[-1]


class OnnxEmbedder(BaseEmbedder):
    name = "onnx"

    def __init__(self, model_dir: Path | None = None, dim: int | None = None) -> None:
        super().__init__()
        self.model_dir = Path(model_dir or (settings.cache_dir / model_dir_name(settings.embedding_model)))
        self.dim = dim or settings.embedding_dim
        self._session = None
        self._tokenizer = None
        self._input_names: set[str] = set()
        self._output_names: list[str] = []

    # ---------- 模型文件 ----------
    def _missing(self) -> list[str]:
        missing = [f for f in SIDECAR_FILES if not (self.model_dir / f).exists()]
        if not (self.model_dir / LOCAL_ONNX).exists():
            missing.append(settings.embedding_onnx_file)
        return missing

    def _download_one(self, client: httpx.Client, remote: str) -> None:
        local = LOCAL_ONNX if "/" in remote else remote
        url = MS_URL.format(repo=settings.embedding_ms_repo, file=remote.replace("/", "%2F"))
        tmp = self.model_dir / (local + ".part")
        with client.stream("GET", url) as resp:
            resp.raise_for_status()
            total = int(resp.headers.get("content-length", 0))
            got = 0
            with tmp.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
                    got += len(chunk)
        if total and got < total * 0.95:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{remote} 下载不完整（{got}/{total}）")
        # 阈值只能挡空响应：config.json / vocab.txt 等小文件本来就只有几百字节，
        # 用 1KB 当下限会把它们全部误判成"内容异常"，导致整条 ONNX 链路降级成哈希。
        if got < 32:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(f"{remote} 下载内容异常（{got} bytes），检查 EMBEDDING_MS_REPO / EMBEDDING_ONNX_FILE")
        tmp.replace(self.model_dir / local)
        logger.info("下载完成 %s -> %s (%.1f MB)", remote, local, got / 1e6)

    def _download_missing(self) -> None:
        missing = self._missing()
        if not missing:
            return
        self.model_dir.mkdir(parents=True, exist_ok=True)
        logger.info("下载模型文件（源 %s）: %s", settings.embedding_ms_repo, missing)
        with httpx.Client(timeout=900, follow_redirects=True) as client:
            for f in missing:
                self._download_one(client, f)

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
            str(self.model_dir / LOCAL_ONNX),
            sess_options=so,
            providers=["CPUExecutionProvider"],
        )
        self._input_names = {i.name for i in self._session.get_inputs()}
        self._output_names = [o.name for o in self._session.get_outputs()]

        self._tokenizer = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=settings.embedding_max_tokens)
        self._tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")

        probe = self._encode(["维度探测"])
        detected = len(probe[0])

        # 维度不一致必须 fail-fast：静默写进 Milvus 会是一堆维度错的垃圾数据，
        # 或者被上层降级成 hash 伪向量而毫无提示。
        if settings.embedding_dim and detected != settings.embedding_dim:
            raise FatalModelError(
                f"模型 {settings.embedding_model} 实际输出 {detected} 维，"
                f"但 EMBEDDING_DIM={settings.embedding_dim}。"
                f"改 EMBEDDING_DIM / MILVUS_DIM 后需要新建 collection。"
            )
        self.dim = detected
        logger.info(
            "onnx embedder ready model=%s dim=%s threads=%s onnx=%s",
            settings.embedding_model,
            self.dim,
            settings.embedding_threads,
            settings.embedding_onnx_file,
        )

    # ---------- 推理 ----------
    def _encode(self, texts: list[str]) -> list[list[float]]:
        """按长度分桶后再分批推理。

        原样整批 encode_batch 会把所有序列 pad 到批内最长的那条：128 条里混进
        一条 400 字，其余 100 字的短块全部要算 4 倍 token，白烧 CPU 时间。
        排序后按 embedding_batch 切块，每块内部长度接近，padding 浪费降到个位数。
        """
        n = len(texts)
        if n == 0:
            return []
        order = sorted(range(n), key=lambda i: len(texts[i]))
        out: list[list[float] | None] = [None] * n
        bs = max(1, settings.embedding_batch)
        for start in range(0, n, bs):
            idx = order[start : start + bs]
            vecs = self._run([texts[i] for i in idx])
            for j, i in enumerate(idx):
                out[i] = vecs[j]
        return out  # type: ignore[return-value]

    def _run(self, texts: list[str]) -> list[list[float]]:
        if self._session is None or self._tokenizer is None:
            raise RuntimeError("embedder 未加载")
        encs = self._tokenizer.encode_batch(texts)
        ids = np.array([e.ids for e in encs], dtype=np.int64)
        mask = np.array([e.attention_mask for e in encs], dtype=np.int64)
        feeds = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._input_names:
            feeds["token_type_ids"] = np.zeros_like(ids)

        outputs = self._session.run(None, feeds)
        hidden = None
        for arr in outputs:
            if arr.ndim == 3:  # last_hidden_state 优先，绕开 pooler_output
                hidden = arr
                break
        if hidden is None:
            hidden = outputs[0]  # 已导出为句向量的模型直接是 [batch, dim]

        vecs = hidden[:, 0, :] if hidden.ndim == 3 else hidden
        # CLS pooling + L2 归一化（归一化后才能直接用 COSINE/IP）
        vecs = np.asarray(vecs, dtype=np.float32)
        norms = np.linalg.norm(vecs, axis=1, keepdims=True)
        vecs = vecs / np.maximum(norms, 1e-9)
        return vecs.tolist()

    def _query_prefix(self) -> str:
        return BGE_ZH_QUERY_PREFIX

    async def health(self) -> dict:
        return {
            "name": self.name,
            "model": settings.embedding_model,
            "dim": self.dim,
            "threads": settings.embedding_threads,
            "onnx": settings.embedding_onnx_file,
            "degraded": False,
            "status": "ok" if self._ready else "loading",
            "path": str(self.model_dir),
        }
