"""检索评测：让"混合检索到底有没有用"变成数字，而不是感觉。

用法
----
  # 1) 用 LLM 从真实切片里造题（一次 5 段、只问"只凭这段能答"的问题）
  uv run python scripts/eval_retrieval.py --novel nv_xxx --build 40

  # 2) 跑基线对比（dense_only vs hybrid）
  uv run python scripts/eval_retrieval.py --novel nv_xxx --modes dense,hybrid

  # 3) 只跑某一份已标注好的问题集
  uv run python scripts/eval_retrieval.py --novel nv_xxx --set data/eval/qa.json

指标口径
--------
Recall@k 按 **父块** 判定：命中块只要和标准答案同属一个 parent 就算召回成功。
原因是我们最终喂给模型的是父块原文，子块是被 dedupe 掉的那一层；
按 chunk_id 严格比对会把"同一段原文的相邻子块"误判成未命中，指标虚低。

延迟口径：p50/p95 只统计检索阶段（不含 LLM 生成），
因为 P1-2 的验收线是"检索 P95 < 300ms"，混进生成时间就没意义了。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app.config import settings  # noqa: E402
from app.db import SessionLocal  # noqa: E402
from app.models import Chunk  # noqa: E402
from app.services.llm.client import get_llm  # noqa: E402


async def build_set(novel_id: str, n: int, out: Path, batch: int = 5) -> None:
    """从切片里抽样造题。gold 记父块 id（见文件头的指标口径）。"""
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(Chunk)
                .where(Chunk.novel_id == novel_id, Chunk.level == 1, Chunk.char_end - Chunk.char_start > 150)
            )
        ).scalars().all()
    if len(rows) < n:
        n = len(rows)
    picks = random.sample(rows, n)
    llm = get_llm()
    items: list[dict] = []

    for start in range(0, len(picks), batch):
        group = picks[start : start + batch]
        excerpts = "\n\n".join(
            f"【片段{i + 1}｜第{c.chapter_no}章】\n{c.content[:600]}" for i, c in enumerate(group)
        )
        prompt = (
            "下面是从一本小说里抽出的若干片段。请为每个片段出一道题。\n"
            "要求：\n"
            "1. 问题必须**只凭该片段**就能回答，不要引入片段外的信息；\n"
            "2. 问题里要带上片段中出现的关键人名/地名/物件，模拟真实读者提问；\n"
            "3. 每个问题 10-30 字，不要出现「根据片段」「这段文字中」这类字样；\n"
            "4. 严格输出 JSON：{\"questions\": [\"问题1\", \"问题2\", ...]}，条数必须等于片段数。\n\n"
            f"{excerpts}"
        )
        try:
            data = await llm.complete_json(
                [{"role": "user", "content": prompt}], temperature=0.3, max_tokens=4000
            )
            qs = data.get("questions") or []
        except Exception as exc:  # noqa: BLE001
            print(f"  造题失败（跳过这 {len(group)} 段）：{exc}")
            continue
        for c, q in zip(group, qs, strict=False):
            if isinstance(q, str) and q.strip():
                items.append(
                    {
                        "question": q.strip(),
                        "gold_parent": c.parent_id or c.id,
                        "gold_chunk": c.id,
                        "chapter_no": c.chapter_no,
                    }
                )
        print(f"  已造 {len(items)} 题")

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"novel_id": novel_id, "items": items}, ensure_ascii=False, indent=2), "utf-8")
    print(f"写入 {out}（{len(items)} 题）")


async def run_eval(novel_id: str, items: list[dict], modes: list[str], k: int) -> dict:
    from app.services.retrieval.hybrid import retrieve

    report: dict[str, dict] = {}
    for mode in modes:
        # 用配置开关模拟三种链路，避免为了评测改出第二套检索代码：
        #   dense  = 纯向量（P0 基线）
        #   rrf    = 双路召回 + RRF，不加精排（隔离出"融合"本身的贡献）
        #   hybrid = 双路召回 + RRF + cross-encoder（P1 目标形态）
        settings.lexical_enabled = mode != "dense"
        settings.rerank_enabled = mode == "hybrid"
        hits = 0
        chunk_hits = 0
        latencies: list[float] = []
        failures: list[dict] = []
        for it in items:
            t0 = time.perf_counter()
            res = await retrieve(novel_id, it["question"], top_k=k)
            latencies.append((time.perf_counter() - t0) * 1000)
            parents = {c.parent_id or c.chunk_id for c in res.citations}
            chunks = {c.chunk_id for c in res.citations}
            ok = it["gold_parent"] in parents
            hits += ok
            chunk_hits += it["gold_chunk"] in chunks
            if not ok:
                failures.append(
                    {"question": it["question"], "gold_chapter": it.get("chapter_no"), "got": len(parents)}
                )
        n = max(len(items), 1)
        lat_sorted = sorted(latencies)
        report[mode] = {
            "recall@k": round(hits / n, 4),
            "chunk_recall@k": round(chunk_hits / n, 4),
            "p50_ms": round(statistics.median(latencies), 1) if latencies else 0,
            "p95_ms": round(lat_sorted[int(len(lat_sorted) * 0.95) - 1], 1) if latencies else 0,
            "n": len(items),
            "misses": failures[:5],
        }
    return report


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--novel", required=True, help="novel_id")
    ap.add_argument("--build", type=int, default=0, help="造题数量（用 LLM）")
    ap.add_argument("--set", default="", help="已有问题集 JSON 路径")
    ap.add_argument("--out", default="data/eval/qa_set.json")
    ap.add_argument("--modes", default="dense,rrf,hybrid", help="逗号分隔：dense,hybrid")
    ap.add_argument("--k", type=int, default=10)
    args = ap.parse_args()

    out = Path(args.out)
    if not out.is_absolute():
        out = Path(__file__).resolve().parent.parent / out

    if args.set:
        items = json.loads(Path(args.set).read_text("utf-8"))["items"]
    elif args.build:
        await build_set(args.novel, args.build, out)
        items = json.loads(out.read_text("utf-8"))["items"]
    else:
        items = json.loads(out.read_text("utf-8"))["items"]

    if not items:
        print("没有题目，先用 --build 造一批")
        return

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    report = await run_eval(args.novel, items, modes, args.k)
    print(json.dumps(report, ensure_ascii=False, indent=2))

    base = report.get("dense", {}).get("recall@k")
    hyb = report.get("hybrid", {}).get("recall@k")
    if base is not None and hyb is not None:
        print(f"\nRecall@{args.k}: dense={base:.3f} → hybrid={hyb:.3f}  ({hyb - base:+.3f})")


if __name__ == "__main__":
    asyncio.run(main())
