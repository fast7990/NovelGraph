"""图谱查询层。所有查询都是单库 SQL，没有图数据库。

**为什么 N 跳邻域用递归 CTE 而不是分步查**：分步查是"每跳一次往返"，
2 跳要两次查询、3 跳三次，且中间结果要传回 Python 再拼成 IN 列表——
节点一多就会撞上参数数量上限。递归 CTE 把整件事交给 PG 一次做完，
查询计划里还能用上 `ix_relations_novel_status`。

递归必须带 `depth < :hops`：只靠 `UNION`（而非 `UNION ALL`）去重是不够的，
`(a,1)` 和 `(a,2)` 是两行不同记录，环上的边会让递归发散。
"""

from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import Text, cast, func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Entity, EntityMention, Relation

logger = logging.getLogger(__name__)

ACTIVE = "active"


async def overview(db: AsyncSession, novel_id: str) -> dict:
    """图谱总览：给管理台做统计卡片和类型分布。"""
    e_total = (await db.execute(select(func.count()).select_from(Entity).where(Entity.novel_id == novel_id))).scalar() or 0
    r_total = (await db.execute(select(func.count()).select_from(Relation).where(Relation.novel_id == novel_id))).scalar() or 0
    r_pending = (
        await db.execute(
            select(func.count()).select_from(Relation).where(Relation.novel_id == novel_id, Relation.status != ACTIVE)
        )
    ).scalar() or 0
    m_total = (
        await db.execute(select(func.count()).select_from(EntityMention).where(EntityMention.novel_id == novel_id))
    ).scalar() or 0

    by_type = (
        await db.execute(
            select(Entity.type, func.count().label("n"), func.sum(Entity.mention_count).label("m"))
            .where(Entity.novel_id == novel_id)
            .group_by(Entity.type)
            .order_by(func.count().desc())
        )
    ).all()

    by_relation = (
        await db.execute(
            select(Relation.relation, func.count().label("n"))
            .where(Relation.novel_id == novel_id, Relation.status == ACTIVE)
            .group_by(Relation.relation)
            .order_by(func.count().desc())
            .limit(15)
        )
    ).all()

    top = (
        await db.execute(
            select(Entity)
            .where(Entity.novel_id == novel_id)
            .order_by(Entity.importance.desc(), Entity.mention_count.desc())
            .limit(20)
        )
    ).scalars().all()

    # 关系类型分布只统计 active：pending 里的 OTHER 占比很高，
    # 混进来会让"关系构成"这张图看起来全是"其他"
    return {
        "entities": e_total,
        "relations": r_total,
        "pending_relations": r_pending,
        "mentions": m_total,
        "by_type": [{"type": t, "count": n, "mentions": int(m or 0)} for t, n, m in by_type],
        "by_relation": [{"relation": r, "count": n} for r, n in by_relation],
        "top_entities": [_entity_brief(e) for e in top],
    }


def _entity_brief(e: Entity) -> dict:
    return {
        "id": e.id,
        "name": e.name,
        "type": e.type,
        "importance": e.importance,
        "mention_count": e.mention_count,
        "first_chapter": e.first_chapter,
        "desc": e.desc,
        "aliases": e.aliases or [],
    }


async def list_entities(
    db: AsyncSession,
    novel_id: str,
    *,
    q: str = "",
    etype: str = "",
    page: int = 1,
    page_size: int = 30,
    order: str = "importance",
) -> dict:
    stmt = select(Entity).where(Entity.novel_id == novel_id)
    count_stmt = select(func.count()).select_from(Entity).where(Entity.novel_id == novel_id)

    if etype:
        stmt = stmt.where(Entity.type == etype)
        count_stmt = count_stmt.where(Entity.type == etype)
    if q:
        # 别名是 jsonb 数组，转文本后模糊匹配够用（实体数量级只有千级，
        # 不需要为"按别名搜索"单独建 GIN 索引）
        like = f"%{q}%"
        # 注意用 sqlalchemy.cast(col, Text) 而不是 func.cast(col, text("text")):
        # 后者生成的是函数调用节点，没有类型信息，接 .ilike() 会抛
        # TypeError: TextClause ... is not a TypeEngine
        cond = or_(Entity.name.ilike(like), cast(Entity.aliases, Text).ilike(like), Entity.desc.ilike(like))
        stmt = stmt.where(cond)
        count_stmt = count_stmt.where(cond)

    order_col = {"importance": Entity.importance, "mentions": Entity.mention_count, "name": Entity.name, "chapter": Entity.first_chapter}.get(
        order, Entity.importance
    )
    stmt = stmt.order_by(order_col.desc() if order != "name" else order_col, Entity.name).offset((page - 1) * page_size).limit(page_size)

    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar() or 0
    return {"items": [_entity_brief(e) for e in rows], "total": total, "page": page, "page_size": page_size}


async def get_entity(db: AsyncSession, novel_id: str, entity_id: str) -> dict | None:
    e = await db.get(Entity, entity_id)
    if e is None or e.novel_id != novel_id:
        return None

    # 出入边一次查完，再用一次查询把对端名字补上（避免 N+1）
    rel_cond = or_(Relation.src_id == entity_id, Relation.dst_id == entity_id)
    # 总数单独查：曹操 343 条、刘备 277 条，原来 limit 200 是静默截断，
    # 前端拿不到总数只能显示"关系 200"，看着像全部，其实是截掉的三分之一。
    rel_total = (
        await db.execute(
            select(func.count()).select_from(Relation).where(Relation.novel_id == novel_id, rel_cond)
        )
    ).scalar() or 0
    rels = (
        await db.execute(
            select(Relation)
            .where(Relation.novel_id == novel_id, rel_cond)
            # 已确认在前，同状态按置信度降序；再按 id 兜底，
            # 否则同分记录在两次查询里顺序可能不同
            .order_by(Relation.status, Relation.confidence.desc(), Relation.id)
            .limit(settings.graph_entity_relations)
        )
    ).scalars().all()

    other_ids = {r.dst_id if r.src_id == entity_id else r.src_id for r in rels}
    others: dict[str, Entity] = {}
    if other_ids:
        rows = (await db.execute(select(Entity).where(Entity.id.in_(other_ids)))).scalars().all()
        others = {x.id: x for x in rows}

    chapters = (
        await db.execute(
            select(EntityMention.chapter_no, func.sum(EntityMention.count).label("n"))
            .where(EntityMention.entity_id == entity_id)
            .group_by(EntityMention.chapter_no)
            .order_by(EntityMention.chapter_no)
        )
    ).all()

    return {
        **_entity_brief(e),
        "relations": [
            {
                "id": r.id,
                "direction": "out" if r.src_id == entity_id else "in",
                "other_id": r.dst_id if r.src_id == entity_id else r.src_id,
                "other_name": getattr(others.get(r.dst_id if r.src_id == entity_id else r.src_id), "name", "?"),
                "relation": r.relation,
                "raw_relation": r.raw_relation,
                "strength": r.strength,
                "confidence": r.confidence,
                "status": r.status,
                "evidence": r.evidence,
                "chapter_no": r.chapter_no,
                "chunk_id": r.chunk_id,
            }
            for r in rels
        ],
        "relation_total": int(rel_total),
        "chapters": [{"chapter_no": int(no), "count": int(n or 0)} for no, n in chapters],
    }


async def subgraph(
    db: AsyncSession,
    novel_id: str,
    *,
    center: str | None = None,
    hops: int = 2,
    max_nodes: int | None = None,
    max_edges: int | None = None,
    include_pending: bool = False,
) -> dict:
    """取一块可渲染的子图。

    没有 `center` 时返回"全书主图"：按 importance 取前 N 个实体当骨架，
    只连这些骨架之间的边——这样即使一本书有几千个实体，首屏也不会糊成一团。
    """
    max_nodes = max_nodes or settings.graph_subgraph_max_nodes
    max_edges = max_edges or settings.graph_subgraph_max_edges
    hops = max(1, min(hops, 4))

    if center:
        node_ids = await _neighbors(db, novel_id, center, hops, max_nodes, include_pending)
        if not node_ids:
            # 孤立实体（没有任何边）：至少把它自己画出来
            node_ids = [center]
        elif center not in node_ids:
            # 双保险：中心节点必须出现在自己的邻域图里
            node_ids = [center, *node_ids]
    else:
        rows = (
            await db.execute(
                select(Entity.id)
                .where(Entity.novel_id == novel_id)
                .order_by(Entity.importance.desc(), Entity.mention_count.desc())
                .limit(max_nodes)
            )
        ).all()
        node_ids = [r[0] for r in rows]

    if not node_ids:
        return {"nodes": [], "edges": [], "center": center, "truncated": False, "node_total": 0}

    node_rows = (await db.execute(select(Entity).where(Entity.id.in_(node_ids)))).scalars().all()
    id_set = {e.id for e in node_rows}

    stmt = select(Relation).where(
        Relation.novel_id == novel_id,
        Relation.src_id.in_(id_set),
        Relation.dst_id.in_(id_set),
    )
    if not include_pending:
        stmt = stmt.where(Relation.status == ACTIVE)
    edges = (
        await db.execute(stmt.order_by(Relation.confidence.desc()).limit(max_edges + 1))
    ).scalars().all()
    truncated = len(edges) > max_edges
    edges = edges[:max_edges]

    return {
        "nodes": [_entity_brief(e) for e in sorted(node_rows, key=lambda x: -x.importance)],
        "edges": [
            {
                "id": r.id,
                "src": r.src_id,
                "dst": r.dst_id,
                "relation": r.relation,
                "strength": r.strength,
                "confidence": r.confidence,
                "status": r.status,
                "chapter_no": r.chapter_no,
            }
            for r in edges
        ],
        "center": center,
        "truncated": truncated,
        "node_total": len(node_rows),
    }


_RECURSIVE_NEIGHBORS = text(
    """
WITH RECURSIVE nb(entity_id, depth) AS (
    SELECT CAST(:seed AS VARCHAR(40)), 0
  UNION
    SELECT CASE WHEN r.src_id = nb.entity_id THEN r.dst_id ELSE r.src_id END,
           nb.depth + 1
    FROM relations r
    JOIN nb ON (r.src_id = nb.entity_id OR r.dst_id = nb.entity_id)
    WHERE r.novel_id = :novel_id
      AND nb.depth < :hops
      AND (:include_pending OR r.status = 'active')
)
SELECT entity_id
FROM (SELECT entity_id, MIN(depth) AS d FROM nb GROUP BY entity_id) t
-- 不带 ORDER BY 的 LIMIT 是"随机砍尾"：邻域超过上限时中心节点会被砍掉，
-- 于是"点开某人"的图里没有他自己。按深度优先保证中心一定在里面。
ORDER BY d, entity_id
LIMIT :max_nodes
"""
)


async def _neighbors(
    db: AsyncSession, novel_id: str, seed: str, hops: int, max_nodes: int, include_pending: bool
) -> list[str]:
    rows = (
        await db.execute(
            _RECURSIVE_NEIGHBORS,
            {
                "seed": seed,
                "novel_id": novel_id,
                "hops": hops,
                "max_nodes": max_nodes,
                "include_pending": include_pending,
            },
        )
    ).all()
    return [r[0] for r in rows]


async def list_relations(
    db: AsyncSession,
    novel_id: str,
    *,
    status: str = "",
    relation: str = "",
    page: int = 1,
    page_size: int = 30,
) -> dict:
    """关系列表。`status=pending` 就是管理台的"待确认"标签页数据源。"""
    stmt = select(Relation).where(Relation.novel_id == novel_id)
    count_stmt = select(func.count()).select_from(Relation).where(Relation.novel_id == novel_id)
    if status:
        stmt = stmt.where(Relation.status == status)
        count_stmt = count_stmt.where(Relation.status == status)
    if relation:
        stmt = stmt.where(Relation.relation == relation)
        count_stmt = count_stmt.where(Relation.relation == relation)

    stmt = stmt.order_by(Relation.confidence.desc()).offset((page - 1) * page_size).limit(page_size)
    rows = (await db.execute(stmt)).scalars().all()
    total = (await db.execute(count_stmt)).scalar() or 0

    ids = {r.src_id for r in rows} | {r.dst_id for r in rows}
    names: dict[str, str] = {}
    if ids:
        for e in (await db.execute(select(Entity.id, Entity.name).where(Entity.id.in_(ids)))).all():
            names[e[0]] = e[1]

    return {
        "items": [
            {
                "id": r.id,
                "src_id": r.src_id,
                "src_name": names.get(r.src_id, "?"),
                "dst_id": r.dst_id,
                "dst_name": names.get(r.dst_id, "?"),
                "relation": r.relation,
                "raw_relation": r.raw_relation,
                "strength": r.strength,
                "confidence": r.confidence,
                "status": r.status,
                "evidence": r.evidence,
                "chapter_no": r.chapter_no,
                "chunk_id": r.chunk_id,
            }
            for r in rows
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


async def relation_counts(db: AsyncSession, novel_id: str) -> dict[str, Any]:
    rows = (
        await db.execute(
            select(Relation.status, func.count()).where(Relation.novel_id == novel_id).group_by(Relation.status)
        )
    ).all()
    return {s: n for s, n in rows}


async def facts_for_entities(
    db: AsyncSession, novel_id: str, names: list[str], *, limit: int | None = None
) -> list[dict]:
    """给 RAG 用的图谱事实：命中实体的一跳关系，转成可注入 prompt 的短句。

    只取一拍（不做多跳）：问答里多跳关系的收益远不如"把这一章到底谁跟谁有关系"
    这件事本身，而多跳会把上下文预算吃光、还容易引出与问题无关的旁支。
    """
    if not names:
        return []
    limit = limit or settings.graph_rag_max_facts

    ents = (
        await db.execute(
            select(Entity).where(Entity.novel_id == novel_id, Entity.name.in_(names)).limit(20)
        )
    ).scalars().all()
    if not ents:
        return []

    ids = [e.id for e in ents]
    name_of = {e.id: e.name for e in ents}
    rels = (
        await db.execute(
            select(Relation)
            .where(
                Relation.novel_id == novel_id,
                Relation.status == ACTIVE,
                or_(Relation.src_id.in_(ids), Relation.dst_id.in_(ids)),
            )
            .order_by(Relation.confidence.desc())
            .limit(limit)
        )
    ).scalars().all()

    other_ids = {r.src_id for r in rels} | {r.dst_id for r in rels}
    if other_ids:
        rows = (await db.execute(select(Entity.id, Entity.name).where(Entity.id.in_(other_ids)))).all()
        for eid, nm in rows:
            name_of.setdefault(eid, nm)

    facts = []
    for r in rels:
        src = name_of.get(r.src_id, "?")
        dst = name_of.get(r.dst_id, "?")
        facts.append(
            {
                "text": f"{src} —{r.relation}→ {dst}",
                "src": src,
                "dst": dst,
                "relation": r.relation,
                "evidence": r.evidence,
                "chapter_no": r.chapter_no,
                "confidence": r.confidence,
            }
        )
    return facts
