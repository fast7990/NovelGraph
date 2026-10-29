"""人工别名表：把规则合不上的称谓焊到同一个人身上。

**为什么需要这个**：`aligner` 的两条合并路径（同名、别名桥）都要求"别名本身
是专名"。`武侯`（武乡侯的简称）、`献帝`（汉献帝的省略）、`陈留王`（刘协登基前的
封号）这些词被 `lexicon` 判为封号/尊称，永远进不了别名表——它们不是模型抽错了，
而是**规则天然覆盖不到**。这类关系要靠知识，靠不了模式。

实测《三国演义》：`诸葛亮` 2432 次提及，旁边挂着 `武侯`(46) 和 `诸葛武侯`(6) 两个
孤立实体；`汉献帝`(12) 与 `献帝`(40)、`陈留王`(24) 三个人各算各的。
116 次提及被切碎在图谱外，检索和子图都拿不全。

**为什么不放代码里**：书是用户的书，人物关系是书里的知识。硬编码《三国》的人物
关系进代码，换一本书就是错的，而且会把"通用逻辑"和"某本书的数据"搅在一起。
所以走配置文件，格式简单到能手工编辑：

```json
{
  "default": { "武侯": "诸葛亮" },
  "nv_ddef13761e244a2f": { "诸葛武侯": "诸葛亮", "献帝": "汉献帝", "陈留王": "汉献帝" }
}
```

`default` 是所有书共用的兜底（同一本书的续篇、同一个作者的作品都可能用），
按 `novel_id` 的条目优先。

**边界**：只做"别名 → 规范名"这一种单向映射，不做链式推导（`A→B`、`B→C` 不会
自动得到 `A→C`）。链式推导是 blob 事故的成因，人工表就该一目了然。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# backend/app/services/graph/manual_aliases.py -> backend/data/graph_aliases.json
DEFAULT_PATH = Path(__file__).resolve().parents[3] / "data" / "graph_aliases.json"

_cache: dict | None = None
_cache_mtime: float = 0.0


def _load(path: Path | None = None) -> dict:
    """读配置。按 mtime 缓存：改完文件不用重启服务，下次建图就生效。"""
    global _cache, _cache_mtime
    p = path or DEFAULT_PATH
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return {}
    if _cache is not None and mtime == _cache_mtime:
        return _cache
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("人工别名表读取失败，按空表处理：%s", p, exc_info=True)
        return {}
    if not isinstance(data, dict):
        logger.warning("人工别名表格式不对（顶层必须是对象），按空表处理：%s", p)
        return {}
    _cache, _cache_mtime = data, mtime
    return data


def aliases_for(novel_id: str, path: Path | None = None) -> dict[str, str]:
    """取某本书生效的人工别名映射（`default` 打底，书级覆盖）。"""
    data = _load(path)
    merged: dict[str, str] = {}
    base = data.get("default")
    if isinstance(base, dict):
        merged.update({str(k): str(v) for k, v in base.items() if k and v})
    own = data.get(novel_id)
    if isinstance(own, dict):
        merged.update({str(k): str(v) for k, v in own.items() if k and v})
    # 自映射没有意义，去掉免得在 align 里多绕一圈
    return {k: v for k, v in merged.items() if k != v}


def upsert(novel_id: str, alias: str, canonical: str, path: Path | None = None) -> None:
    """写回一条映射（`scripts/merge_entities.py --write-config` 用）。

    保留文件里其他内容、保持 2 空格缩进和中文不转义——这个文件是给人看的，
    `ensure_ascii=True` 会把它变成一串 \\uXXXX，等于不能编辑。
    """
    p = path or DEFAULT_PATH
    data = _load(p) if p.exists() else {}
    data = dict(data)
    bucket = data.get(novel_id)
    if not isinstance(bucket, dict):
        bucket = {}
    bucket[alias] = canonical
    data[novel_id] = bucket
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(
        json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    global _cache, _cache_mtime
    _cache = None  # 下次 _load 重新读
