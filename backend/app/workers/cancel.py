"""入库任务取消信号（单进程内存版）。

为什么需要：删书接口只删了 DB 和向量，worker 线程还在按 chunk 列表继续 embed，
一本几百万字的书能白烧一个多小时 CPU，还占住串行队列让后面的任务一直排队。
长耗时循环（解析 / 向量化）每批检查一次这个集合，命中就抛 IngestCancelled 就地退出。

单进程单 worker，用普通 set 就够；将来换 Celery 时替换成广播信号即可。
"""

from __future__ import annotations

_cancelled: set[str] = set()


class IngestCancelled(Exception):
    """入库被主动取消（通常是这本书已经被删了）。"""

    def __init__(self, novel_id: str) -> None:
        super().__init__(f"入库已取消：novel={novel_id}")
        self.novel_id = novel_id


def cancel(novel_id: str) -> None:
    _cancelled.add(novel_id)


def clear(novel_id: str) -> None:
    _cancelled.discard(novel_id)


def is_cancelled(novel_id: str) -> bool:
    return novel_id in _cancelled
