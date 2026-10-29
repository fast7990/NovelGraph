"""Query 意图路由（P0）：无关 query 拦截。

只做"无关拦截"这一级，不做多分类路由。原因（与 rag.py 顶部说明一致）：
- 小说问答的 query 类型收敛（事实定位 / 人物关系 / 概览），重路由动的是外围编排，
  碰不到真正拉开差距的检索质量（bigram / RRF / rerank）；
- 路由本身会出错——把该检索的误判成"无关"比无差别检索更糟。
  所以**默认 fallback 到检索**，只有高置信度的纯闲聊 / 无内容 / 明确离题短句
  才判 `IRRELEVANT`。

纯规则、零依赖、零额外大模型 round-trip（不增加首字延迟）：
- 整句都是问候 / 客套 / 无实质内容 → 不检索、不生成，直接回礼貌话术；
- 短句且命中明确离题关键词（且未提及"本书"）→ 同样拦截。
其余一律放行走现有检索链路，宁放过不误杀。
"""

from __future__ import annotations

import re
from enum import Enum

from app.config import settings


class Intent(str, Enum):
    IRRELEVANT = "irrelevant"   # 不检索、不生成，直接回礼貌话术
    FACTUAL = "factual"         # 默认：走现有检索链路


# 问候 / 客套 / 在场确认类。整句去掉这些词后若不再有内容，判为无关。
_GREETINGS = {
    "你好", "您好", "哈喽", "嗨", "hi", "hello", "hey", "在吗", "在不在", "有人吗",
    "在的", "在的哦", "嗯嗯", "哦哦", "好的", "好的呢", "谢谢", "感谢", "多谢",
    "再见", "拜拜", "拜拜了", "晚安", "早安", "早", "午安",
}

# 单字语气词：去掉它们再判断是否有实质内容
_FILLERS = set("啊呀呢嘛哦啦哈诶喂勒咯嘞呦呗喽喔额")

# 明确的离题短句关键词：这些话题不可能从小说正文答出。
# 用"短句（<=12 字）+ 命中关键词 + 未提及本书"三重约束，避免误杀
# "书里写天气的句子有哪些"这类合法问法（见 _BOOK_HINTS）。
_OFFTOPIC_KEYWORDS = (
    "天气", "讲个笑话", "讲笑话", "冷笑话", "写代码", "编程",
    "你是谁", "你叫什么", "几点了", "现在几点", "今天几号",
    "什么星座", "帮我算", "翻译一下", "翻译成英语", "怎么下载",
)

# 一旦命中这些词，说明问的是"本书内容"，即使包含离题关键词也要放行去检索。
_BOOK_HINTS = (
    "书", "小说", "本章", "这章", "那章", "里面", "文中", "原文",
    "主角", "角色", "剧情", "情节", "设定", "作者", "第几章", "哪一章",
)

_GREETING_REPLY = "你好！我是这本书的专属问答助手，关于本书的情节、人物、设定都可以问我～"
_OFFTOPIC_REPLY = "我是这本小说的专属问答助手，只能回答和本书内容相关的问题哦。换个关于这本书的问题试试？"


def classify(question: str) -> tuple[Intent, str]:
    """返回 (意图, 无关时给前端的礼貌话术；非无关时为空串)。

    判定顺序（任一命中即 IRRELEVANT，否则 FACTUAL）：
    1. 去掉标点 / 空白后为空 → 无内容；
    2. 去掉问候词 + 语气词后什么也不剩 → 纯闲聊；
    3. 短句（<=12 字）且命中离题关键词且未提及本书 → 明确离题。
    """
    if not settings.intent_routing_enabled:
        return Intent.FACTUAL, ""
    q = (question or "").strip().lower()
    if not q:
        return Intent.IRRELEVANT, _GREETING_REPLY

    # 去掉所有非中英数字符（标点、空白、emoji 等），只留"字"
    core = re.sub(r"[\W_]+", "", q)

    # 1) 纯问候 / 客套：去掉它们后若什么都不剩，就是闲聊
    core_after_greet = core
    for g in _GREETINGS:
        core_after_greet = core_after_greet.replace(g, "")
    core_after_greet = "".join(c for c in core_after_greet if c not in _FILLERS)
    if core_after_greet == "":
        return Intent.IRRELEVANT, _GREETING_REPLY

    # 2) 明确离题的短句：短 + 命中关键词 + 没在聊"这本书" → 几乎不可能是合法提问
    mentions_book = any(h in q for h in _BOOK_HINTS)
    if not mentions_book and len(core) <= 12 and any(kw in q for kw in _OFFTOPIC_KEYWORDS):
        return Intent.IRRELEVANT, _OFFTOPIC_REPLY

    return Intent.FACTUAL, ""
