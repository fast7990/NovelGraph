"""倒数排名融合（RRF）。

为什么不用加权分数
------------------
稠密通道是 cosine（0–1 有界），词法通道是 BM25（无界、随语料长度漂）。
想加权就必须先归一化，而归一化参数一旦换 embedding 模型、换分词策略、
甚至只是新增一本书就会失效——线上表现为"调好的权重过两周突然不灵了"。

RRF 只看排名不看分值：score(d) = Σ 1 / (k + rank_c(d))。
换模型不用重调参，这是它能成为默认融合策略的根本原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Fused:
    chunk_id: str
    score: float = 0.0
    ranks: dict[str, int] = field(default_factory=dict)  # 通道名 → 该通道内的名次(1 起)
    raw: dict[str, float] = field(default_factory=dict)  # 通道名 → 原始分值，仅用于展示排查


def rrf(
    channels: dict[str, list[tuple[str, float]]],
    k: int = 60,
    weights: dict[str, float] | None = None,
) -> list[Fused]:
    """channels: {通道名: [(chunk_id, 原始分), …]}，每路内部按相关度降序。

    同一个 chunk 被多路命中会累加——这正是 RRF 的核心：
    两路都排前面的文档，比只在单路排第一的文档更可信。
    """
    weights = weights or {}
    acc: dict[str, Fused] = {}
    for name, items in channels.items():
        w = weights.get(name, 1.0)
        for rank, (cid, raw) in enumerate(items, start=1):
            f = acc.get(cid)
            if f is None:
                f = Fused(chunk_id=cid)
                acc[cid] = f
            f.score += w / (k + rank)
            f.ranks[name] = rank
            f.raw[name] = raw
    return sorted(acc.values(), key=lambda x: x.score, reverse=True)
