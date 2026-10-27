"""精排（cross-encoder）接口。

精排和向量召回是两种不同的东西，别混为一谈：
- 双塔（embedding）把 query 和 doc 各自编码成向量再算余弦，doc 之间互不知情，
  所以可以离线建索引、毫秒级召回，但**看不词语之间的细粒度交互**；
- cross-encoder 把 [query, doc] 拼起来一起过一遍模型，能真正判断"这段是否回答了问题"，
  代价是不能预计算，只能对少量候选（这里 50 条）现算。

所以顺序永远是：粗排召回（两路）→ 融合 → 精排，不是拿精排去替代召回。
"""

from __future__ import annotations

from typing import Protocol


class BaseReranker(Protocol):
    name: str
    ready: bool

    async def rerank(self, query: str, docs: list[str], top_n: int = 24) -> list[float]:
        """返回与 docs 等长的相关度分数（越大越相关），调用方自行排序。"""
        ...
