"""把图谱里已经存在的重复实体合并掉（存量数据修复）。

和 `manual_aliases.py` 的分工：
- **配置表**管"下次重建时别再分裂"（`aligner` 读它）；
- **本脚本**管"现在库里已经分裂的怎么办"——重建图谱要几十分钟 LLM，
  为了两条别名不值得。

用法：
    # 先看会发生什么，不写库
    ./.venv/Scripts/python.exe scripts/merge_entities.py --novel nv_xxx \
        --from 武侯 --to 诸葛亮 --dry-run

    # 确认无误后执行，并把这条关系写进配置表
    ./.venv/Scripts/python.exe scripts/merge_entities.py --novel nv_xxx \
        --from 武侯 --to 诸葛亮 --write-config

合并要做四件事，漏一件数据就丢：
1. `entity_mentions` 改指目标实体（同一 chunk 撞唯一键时累加 count，不是覆盖）
2. `relations` 的 src/dst 改指目标（改完 src==dst 的自环边删掉，它没有信息量）
3. 源实体名进目标实体的 `aliases`（否则下次回填关系又对不上）
4. 目标 mention_count / importance 重算，源实体删除

`importance` 必须重算而不是相加：它是按提及频次对数缩放的**归一化**值，
相加会溢出 1，前端节点大小直接失真。
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import delete, func, select, text

from app.db import SessionLocal
from app.models import Entity, EntityMention, Relation
from app.services.graph import manual_aliases


async def _find(db, novel_id: str, name: str) -> list[Entity]:
    rows = (
        await db.execute(
            select(Entity).where(Entity.novel_id == novel_id, Entity.name == name)
        )
    ).scalars().all()
    return list(rows)


async def merge_one(
    db, novel_id: str, src_name: str, dst_name: str, *, dry_run: bool
) -> bool:
    srcs = await _find(db, novel_id, src_name)
    dsts = await _find(db, novel_id, dst_name)
    if not srcs:
        print(f"  跳过：找不到实体 {src_name!r}")
        return False
    if not dsts:
        print(f"  跳过：找不到目标实体 {dst_name!r}（目标必须已存在，脚本不负责凭空建）")
        return False
    if len(dsts) > 1:
        print(f"  跳过：{dst_name!r} 有 {len(dsts)} 个（不同类型），请在配置里明确类型")
        return False

    dst = dsts[0]
    for src in srcs:
        if src.id == dst.id:
            continue
        if src.type != dst.type:
            print(f"  跳过：{src_name!r} 是 {src.type}，{dst_name!r} 是 {dst.type}，类型不同不合并")
            continue

        m_src = await db.scalar(
            select(func.count()).select_from(EntityMention).where(EntityMention.entity_id == src.id)
        )
        r_src = await db.scalar(
            select(func.count())
            .select_from(Relation)
            .where((Relation.src_id == src.id) | (Relation.dst_id == src.id))
        )
        print(
            f"  {src.name}({src.id}) → {dst.name}({dst.id})  "
            f"提及 {src.mention_count} 条 / mention 行 {m_src} / 关系 {r_src} 条"
        )
        if dry_run:
            continue

        # 1) mention 改指。唯一键是 (entity_id, chunk_id)，两边都提过同一个 chunk 会撞，
        #    所以先 upsert 再删源行，不能直接 UPDATE（会撞约束）
        await db.execute(
            text(
                """
                insert into entity_mentions (id, novel_id, entity_id, chapter_no, chunk_id, count)
                select md5(random()::text || clock_timestamp()::text), novel_id, :dst,
                       chapter_no, chunk_id, count
                from entity_mentions where entity_id = :src
                on conflict (entity_id, chunk_id)
                do update set count = entity_mentions.count + excluded.count
                """
            ),
            {"dst": dst.id, "src": src.id},
        )
        await db.execute(delete(EntityMention).where(EntityMention.entity_id == src.id))

        # 2) 关系改指。
        #    不能直接 UPDATE：唯一键是 (novel_id, src_id, dst_id, relation)，
        #    A-刘备 和 B-刘备 并存时，A 改成 B 就撞约束了。走 upsert：
        #    冲突时保留更高的 confidence / 更长的证据文本。
        for col in ("src_id", "dst_id"):
            new_src = ":dst" if col == "src_id" else "src_id"
            new_dst = ":dst" if col == "dst_id" else "dst_id"
            await db.execute(
                text(
                    f"""
                    insert into relations (
                        id, novel_id, src_id, dst_id, relation, raw_relation,
                        strength, confidence, status, evidence, chapter_no, chunk_id, created_at
                    )
                    select md5(random()::text || clock_timestamp()::text), novel_id,
                           {new_src}, {new_dst},
                           relation, raw_relation, strength, confidence, status,
                           evidence, chapter_no, chunk_id, created_at
                    from relations
                    where novel_id = :n and {col} = :src
                    on conflict (novel_id, src_id, dst_id, relation) do update set
                        confidence = greatest(relations.confidence, excluded.confidence),
                        strength   = greatest(relations.strength, excluded.strength),
                        evidence   = case
                                       when length(excluded.evidence) > length(relations.evidence)
                                       then excluded.evidence else relations.evidence end,
                        chunk_id   = coalesce(relations.chunk_id, excluded.chunk_id),
                        raw_relation = case
                                       when relations.raw_relation = 'OTHER' then excluded.raw_relation
                                       else relations.raw_relation end
                    """
                ),
                {"dst": dst.id, "src": src.id, "n": novel_id},
            )
        await db.execute(
            delete(Relation).where(
                Relation.novel_id == novel_id,
                (Relation.src_id == src.id) | (Relation.dst_id == src.id),
            )
        )
        # 自环没有信息量（"诸葛亮-诸葛亮"），留着只会让子图出现莫名其妙的圈
        dropped = await db.execute(
            delete(Relation).where(
                Relation.novel_id == novel_id,
                Relation.src_id == dst.id,
                Relation.dst_id == dst.id,
            )
        )
        if dropped.rowcount:
            print(f"    删除自环关系 {dropped.rowcount} 条")

        # 3) 别名 + 4) 计数
        aliases = list(dst.aliases or [])
        for name in (src.name, *(src.aliases or [])):
            if name and name != dst.name and name not in aliases:
                aliases.append(name)
        dst.aliases = aliases
        dst.mention_count = int(dst.mention_count or 0) + int(src.mention_count or 0)
        dst.first_chapter = min(
            [x for x in (dst.first_chapter, src.first_chapter) if x] or [0]
        )
        if len(src.desc or "") > len(dst.desc or ""):
            dst.desc = src.desc

        await db.delete(src)
        await db.flush()
        print(f"    已合并。目标现提及 {dst.mention_count}，别名 {len(aliases)} 个")

    if not dry_run:
        # importance 是全书的归一化值：合并改变了总提及分布，必须统一重算。
        # 用和 builder 一致的公式（log 缩放），否则新旧实体的节点大小不可比
        await db.execute(
            text(
                """
                with m as (
                    select id, mention_count,
                           ln(1 + greatest(mention_count, 0)) as v
                    from entities where novel_id = :n
                ), mm as (select max(v) as vmax from m)
                update entities e
                set importance = round((m.v / nullif(mm.vmax, 0))::numeric, 4)
                from m, mm
                where e.id = m.id
                """
            ),
            {"n": novel_id},
        )
        await db.commit()
    return True


async def main() -> None:
    ap = argparse.ArgumentParser(description="合并图谱重复实体")
    ap.add_argument("--novel", required=True, help="novel_id")
    ap.add_argument("--from", dest="src", required=True, action="append", help="要并入的称谓，可重复")
    ap.add_argument("--to", dest="dst", required=True, help="目标规范名")
    ap.add_argument("--dry-run", action="store_true", help="只打印，不写库")
    ap.add_argument("--write-config", action="store_true", help="把 from→to 写进人工别名表")
    args = ap.parse_args()

    print(f"小说 {args.novel}：合并到 {args.dst!r}")
    async with SessionLocal() as db:
        ok = False
        for src in args.src:
            ok = await merge_one(db, args.novel, src, args.dst, dry_run=args.dry_run) or ok
        if args.dry_run:
            await db.rollback()
            print("\n[dry-run] 未写库。去掉 --dry-run 才会真正合并。")

    if ok and args.write_config:
        for src in args.src:
            manual_aliases.upsert(args.novel, src, args.dst)
            print(f"已写入人工别名表：{src} → {args.dst}")


if __name__ == "__main__":
    asyncio.run(main())
