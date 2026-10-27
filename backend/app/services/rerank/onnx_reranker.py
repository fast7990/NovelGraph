"""ONNX cross-encoder 精排（默认 bge-reranker-base，int8）。

模型：ModelScope `Xenova/bge-reranker-base` 的 `onnx/model_int8.onnx`（279MB）。
输入是 `[query, doc]` 拼成的序列，输出一个 logit，sigmoid 后就是"相关"概率。

工程上三个必须做对的点
1. **批量**：50 条候选逐条跑会慢 5 倍以上，必须一次 pad 成矩阵。
2. **线程不超配**：和 embedding 抢同一批核，inter_op 固定 1、intra_op 用配置值。
3. **失败必须可降级**：模型没下好/加载失败时不能让问答整个挂掉，
   返回 ready=False，由检索层退回纯 RRF 排序（并记录 mode）。
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import httpx
import numpy as np

from app.config import settings
from app.services.embedding.onnx_embedder import MS_URL, model_dir_name

logger = logging.getLogger(__name__)

LOCAL_ONNX = "model.onnx"
SIDECAR = ["tokenizer.json", "tokenizer_config.json", "special_tokens_map.json", "config.json"]
_CHUNK = 4 << 20  # 4MB 一片，8 路并发


def _remote_url(repo: str, file: str) -> str:
    return MS_URL.format(repo=repo, file=file.replace("/", "%2F"))


def _download_parallel(repo: str, file: str, dest: Path, parts: int = 8) -> None:
    """ModelScope 单连接只有几百 KB/s，大文件必须切片并发拉。"""
    url = _remote_url(repo, file)
    head = httpx.head(url, follow_redirects=True, timeout=60)
    head.raise_for_status()
    total = int(head.headers.get("content-length", 0))
    if total < 8 << 20 or head.headers.get("accept-ranges", "").lower() != "bytes":
        with httpx.stream("GET", url, follow_redirects=True, timeout=900) as resp:
            resp.raise_for_status()
            with dest.open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)
        return

    step = total // parts + 1
    tmp = dest.with_suffix(".part")
    tmp.mkdir(parents=True, exist_ok=True)

    def _one(i: int) -> None:
        start = i * step
        end = min(start + step, total) - 1
        if start > end:
            return
        with httpx.stream(
            "GET", url, headers={"Range": f"bytes={start}-{end}"}, follow_redirects=True, timeout=900
        ) as resp:
            resp.raise_for_status()
            with (tmp / f"{i:02d}").open("wb") as fh:
                for chunk in resp.iter_bytes(1 << 20):
                    fh.write(chunk)

    # 线程池拉，避免 asyncio 事件循环在这一步被占住
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=parts) as pool:
        list(pool.map(_one, range(parts)))

    with dest.open("wb") as out:
        for i in range(parts):
            part = tmp / f"{i:02d}"
            if part.exists():
                out.write(part.read_bytes())
                part.unlink()
    tmp.rmdir()
    if dest.stat().st_size < total * 0.98:
        size = dest.stat().st_size
        dest.unlink(missing_ok=True)
        raise RuntimeError(f"reranker 模型下载不完整（{size}/{total}）")


class OnnxReranker:
    name = "onnx"

    def __init__(self) -> None:
        self.model_dir = Path(settings.rerank_cache_dir) / model_dir_name(settings.rerank_model)
        if not self.model_dir.is_absolute():
            self.model_dir = (Path.cwd() / self.model_dir).resolve()
        self.ready = False
        self.error = ""
        self._session = None
        self._tok = None
        self._in_names: list[str] = []

    # ---------- 加载 ----------
    def _missing(self) -> list[str]:
        miss = [f for f in SIDECAR if not (self.model_dir / f).exists()]
        if not (self.model_dir / LOCAL_ONNX).exists():
            miss.append(settings.rerank_onnx_file)
        return miss

    def load_sync(self) -> None:
        import onnxruntime as ort
        from tokenizers import Tokenizer

        self.model_dir.mkdir(parents=True, exist_ok=True)
        missing = self._missing()
        if missing:
            logger.info("reranker 模型缺失，开始下载（%s）: %s", settings.rerank_ms_repo, missing)
            for f in missing:
                local = LOCAL_ONNX if "/" in f else f
                dest = self.model_dir / local
                if dest.exists():
                    continue
                logger.info("下载 %s …", f)
                _download_parallel(settings.rerank_ms_repo, f, dest)
                logger.info("下载完成 %s (%.1f MB)", local, dest.stat().st_size / 1e6)

        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, settings.rerank_threads)
        so.inter_op_num_threads = 1
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self._session = ort.InferenceSession(
            str(self.model_dir / LOCAL_ONNX), sess_options=so, providers=["CPUExecutionProvider"]
        )
        self._in_names = [i.name for i in self._session.get_inputs()]

        self._tok = Tokenizer.from_file(str(self.model_dir / "tokenizer.json"))
        self._tok.enable_truncation(max_length=settings.rerank_max_length)
        self.ready = True
        logger.info("reranker 就绪：%s（输入 %s）", settings.rerank_model, self._in_names)

    async def warmup(self) -> None:
        try:
            await asyncio.to_thread(self.load_sync)
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            logger.warning("reranker 加载失败，检索将退回 RRF 排序：%s", self.error)

    # ---------- 推理 ----------
    def _run(self, query: str, docs: list[str]) -> list[float]:
        if not docs:
            return []
        assert self._tok is not None and self._session is not None
        encs = [self._tok.encode(query, d) for d in docs]
        max_len = max(len(e.ids) for e in encs)
        ids = np.zeros((len(encs), max_len), dtype=np.int64)
        mask = np.zeros((len(encs), max_len), dtype=np.int64)
        for i, e in enumerate(encs):
            n = len(e.ids)
            ids[i, :n] = e.ids
            mask[i, :n] = e.attention_mask
        feed = {"input_ids": ids, "attention_mask": mask}
        if "token_type_ids" in self._in_names:
            feed["token_type_ids"] = np.zeros_like(ids)
        if "position_ids" in self._in_names:
            feed["position_ids"] = np.tile(np.arange(max_len, dtype=np.int64), (len(encs), 1))
        logits = self._session.run(None, feed)[0]
        flat = np.asarray(logits, dtype=np.float32).reshape(len(encs), -1)[:, 0]
        # 二分类单 logit 用 sigmoid；若模型输出已是概率（0-1）也兼容
        if flat.min() >= 0.0 and flat.max() <= 1.0:
            return [float(x) for x in flat]
        return [float(1.0 / (1.0 + np.exp(-x))) for x in flat]

    async def rerank(self, query: str, docs: list[str], top_n: int = 24) -> list[float]:
        return await asyncio.to_thread(self._run, query, docs)

    async def health(self) -> dict:
        return {
            "name": "rerank",
            # 和 /health 其它依赖保持同一套 status 语义：
            # 未就绪只是降级（检索退回 RRF 排序），不影响服务可用性
            "status": "ok" if self.ready else "degraded",
            "model": settings.rerank_model,
            "ready": self.ready,
            "error": self.error,
            "dir": str(self.model_dir),
        }
