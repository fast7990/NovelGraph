"""检索耗时分解：一次提问为什么要在「正在检索资料」上等一分钟。

冒烟时发现 /search 单次要 60s+，但 pgvector 本地查询、PG 词法都是毫秒级，
嫌疑集中在「远程 embedding」和「本地 ONNX 精排」两处。这个脚本逐段计时，
把 60 秒归到具体那一段，免得凭猜去调参。

跑法：backend/.venv/Scripts/python.exe scripts/profile_retrieval.py
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import settings  # noqa: E402
from app.services.embedding.factory import get_embedder  # noqa: E402
from app.services.rerank.factory import get_reranker  # noqa: E402
from app.services.retrieval import lexical  # noqa: E402
from app.services.retrieval.hybrid import retrieve  # noqa: E402
from app.services.vector.factory import get_vector_store  # noqa: E402

NOVEL = "nv_ddef13761e244a2f"
Q = "关羽的性格"


def mark(name: str, t0: float) -> float:
    dt = time.perf_counter() - t0
    print(f"  {name:22s} {dt:7.2f}s")
    return time.perf_counter()


async def main() -> None:
    print(f"embedding={settings.embedding_provider} rerank={settings.rerank_enabled}")
    t = time.perf_counter()
    emb = get_embedder()
    t = mark("embedder 初始化", t)

    vec = await emb.embed_query(Q)
    t = mark("embed_query（远程）", t)

    store = await get_vector_store()
    t = mark("vector_store 初始化", t)

    hits = await store.search(NOVEL, vec, top_k=50, level=1)
    t = mark(f"pgvector 检索（{len(hits)} 条）", t)

    lex = await lexical.search(NOVEL, Q, limit=50)
    t = mark(f"词法检索（{len(lex)} 条）", t)

    rr = get_reranker()
    # 懒加载：不 warmup 的话 ready 永远是 False，量不到精排的真实开销
    await rr.warmup()
    print(f"  reranker ready={rr.ready} err={getattr(rr, 'error', '')}")
    if rr.ready:
        docs = ["关羽字云长，河东解良人也。身长九尺，面如重枣，唇若涂脂。" * 40] * 50
        await rr.rerank(Q, docs, top_n=24)
        t = mark("精排 50 条（首次）", t)
        await rr.rerank(Q, docs, top_n=24)
        t = mark("精排 50 条（热态）", t)

    t = time.perf_counter()
    r = await retrieve(NOVEL, Q, top_k=8)
    mark(f"retrieve 全流程（{len(r.citations)} 条）", t)


if __name__ == "__main__":
    asyncio.run(main())
