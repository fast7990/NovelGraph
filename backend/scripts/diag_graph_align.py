"""诊断：图谱实体未对齐（blob 化）的量化证据 + 抽取→对齐链路的复现。

分两段：
1. **统计**：看持久化的图里"泛称/尊称别名"造成了多大的合并（哪些实体被并成了 blob、
   别名是否被 12 条上限截断、提及量是不是被泛称词灌水）。
2. **复现**：拿真实父块跑一遍 L2 抽取 → L3 对齐，确认抽取阶段本身是好的，
   问题出在别名与合并规则上。

跑法：cd backend && uv run python scripts/diag_graph_align.py [--no-llm]
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.services.graph.aligner import align  # noqa: E402
from app.services.graph.candidates import extract_candidates  # noqa: E402
from app.services.graph.extractor import ExtractedEntity, extract_chunk  # noqa: E402

N = "nv_ddef13761e244a2f"  # 三国演义

# 这些词在书里指向很多人，作为别名出现就是把两个人焊在一起的桥
GENERIC = (
    "丞相", "都督", "主公", "明公", "天子", "大王", "将军", "大人", "先生",
    "贤弟", "公", "君", "陛下", "皇帝", "太师", "相国", "元帅", "太守", "刺史",
)
PROBES = ("曹操", "刘备", "关羽", "诸葛亮")


async def stats_section() -> None:
    async with SessionLocal() as db:

        async def q(sql: str, **kw):
            return (await db.execute(text(sql), kw)).all()

        print("== 泛称实体（以尊称/官职为 name 的实体）==")
        rows = await q(
            """select name, type, mention_count from entities
               where novel_id=:n and name = any(:g) order by mention_count desc""",
            n=N,
            g=list(GENERIC),
        )
        print("  ", rows or "（没有：说明泛称都被当成了别人的别名，而不是独立实体）")

        print("\n== 别名里含泛称的实体（合并桥点）==")
        rows = await q(
            """select name, mention_count, aliases from entities
               where novel_id=:n and exists (
                  select 1 from jsonb_array_elements_text(aliases) a where a = any(:g)
                ) order by mention_count desc limit 12""",
            n=N,
            g=list(GENERIC),
        )
        for name, mc, aliases in rows:
            print(f"  {name}({mc}) aliases={aliases}")

        print("\n== 别名被 12 条上限截断的实体 ==")
        rows = await q(
            """select name, mention_count, jsonb_array_length(aliases) l from entities
               where novel_id=:n and jsonb_array_length(aliases) >= 12
               order by mention_count desc""",
            n=N,
        )
        print("  ", rows)

        print("\n== 提及量前 8 的实体（看提及量是不是被泛称词灌水）==")
        rows = await q(
            """select name, mention_count, aliases from entities where novel_id=:n
               order by mention_count desc limit 8""",
            n=N,
        )
        for name, mc, aliases in rows:
            print(f"  {name}({mc}) aliases={aliases}")

        print("\n== 全书关键字的真实出现次数（对照上表即知灌水幅度）==")
        contents = await q("select content from chunks where novel_id=:n and level=0", n=N)
        big = "".join(r[0] for r in contents)
        for w in ("曹操", "刘备", "诸葛亮", "关羽", "丞相", "将军", "主公", "都督", "天子", "孔明", "云长"):
            print(f"  {w}: {big.count(w)}")

        print("\n== 关键实体在不在（重建后的硬指标）==")
        rows = await q(
            """select name, mention_count, aliases from entities
               where novel_id=:n and name = any(:p) order by mention_count desc""",
            n=N,
            p=list(PROBES),
        )
        found = {r[0] for r in rows}
        for probe in PROBES:
            hit = next((r for r in rows if r[0] == probe), None)
            mark = "在" if probe in found else "❌ 消失"
            print(f"  {probe}: {mark}" + (f"  mention={hit[1]} aliases={hit[2]}" if hit else ""))

        print("\n== 提及量 > 800 的实体（blob 检查，重建后应该没有）==")
        rows = await q(
            """select name, mention_count from entities
               where novel_id=:n and mention_count > 800 order by mention_count desc""",
            n=N,
        )
        print("  ", rows or "（无）")


async def repro_section(chapters: tuple[int, ...]) -> None:
    from app.services.llm.client import get_llm

    llm = get_llm()
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                text(
                    """select id, chapter_no, content from chunks
                       where novel_id=:n and level=0 and chapter_no = any(:ch)
                       order by chapter_no, seq"""
                ),
                {"n": N, "ch": list(chapters)},
            )
        ).all()

    by_ch: dict[int, list] = defaultdict(list)
    for r in rows:
        by_ch[r[1]].append(r)

    acc: list[ExtractedEntity] = []
    for chno, blocks in by_ch.items():
        known = extract_candidates("".join(b[2] for b in blocks), top_n=40)
        for _cid, _cno, content in blocks:
            res = await extract_chunk(llm, content, chapter_no=chno, known_names=known)
            acc.extend(res.entities)
            for e in res.entities:
                if e.type == "Character" and e.name not in known:
                    known.append(e.name)

    print(f"\n== 复现：第 {list(chapters)} 章，原始实体 {len(acc)} 条 ==")
    for probe in PROBES:
        hits = [e for e in acc if e.name == probe]
        print(f"  抽取阶段 {probe}: {len(hits)} 次  aliases={[h.aliases for h in hits][:3]}")

    text_all = "".join(b[2] for _no, bl in by_ch.items() for b in bl)
    mentions = {f"Character\x00{n}": text_all.count(n) for n in {e.name for e in acc}}
    aligned = align(acc, mentions=mentions)
    names = {e.name for e in aligned.entities}
    print(f"  对齐后 {len(aligned.entities)} 个实体")
    for probe in PROBES:
        print(f"  对齐后 {probe}: {'在' if probe in names else '没了'}")
    print("  合并对:", aligned.merged_pairs[:20])


async def _run(no_llm: bool) -> None:
    # 必须同一个 event loop：asyncpg 的连接池绑在创建它的 loop 上，
    # 分两次 asyncio.run 会在 Windows proactor 上抛 'NoneType' has no attribute 'send'
    await stats_section()
    if not no_llm:
        await repro_section((5, 21))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-llm", action="store_true", help="只跑统计，不调模型")
    args = ap.parse_args()
    asyncio.run(_run(args.no_llm))


if __name__ == "__main__":
    main()
