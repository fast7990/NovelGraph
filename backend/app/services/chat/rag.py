"""RAG 问答：检索 → 组装 prompt → 流式生成 → 带引用落库。

P1 起多了一路**图谱事实**：问题里提到的实体，把它的一跳关系作为短句注入 prompt。
和向量检索是两条互补的路——向量找的是"这段话在哪"，图谱给的是"这两个人是什么关系"。
后者对"谁和谁是什么关系""某某的师父是谁"这类问题，比再多召回十条切片都直接。

P1-M7 起多了一路**上下文记忆**（见 `memory.py`）：
历史不再只是塞进 prompt，而是分成三份用——
  1. 历史对话压成一段文本进 prompt（让模型知道上文说了什么）；
  2. 历史里的实体继承进检索 query（让召回有锚点，见 `_expand_query`）；
  3. 历史实体一并喂给图谱事实抽取（"他的师父是谁"能定位到人）。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import Entity, Novel
from app.services.chat import memory
from app.services.chat.intent import Intent, classify
from app.services.graph import store as graph_store
from app.services.llm.client import get_llm
from app.services.retrieval.hybrid import build_context, retrieve

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位严谨的小说内容助手。请严格依据【资料】回答用户问题。

规则：
1. 只使用【资料】中的信息。资料没有提到的事实，直接说明"资料中未提及"，不要推测。
2. 回答末尾用 [资料N] 标注依据；多个来源写成 [资料1][资料3]。
3. 涉及人物、时间、地点的细节必须与原文一致，不要改写专有名词。
4. 用中文回答，条理清晰，一般不超过 400 字；用户要求详细时再展开。
5. 如果用户问的是"这本书讲了什么"这类总览问题，按章节顺序概括，不要编造结局。
6. 若给出了【人物关系】，可以用它理清人物之间的称呼与阵营，但**它与【资料】冲突时以【资料】为准**。
   人物关系里没写的内容不要当作事实补充进答案。
7. 【历史对话】是用户与助手此前的对话，只用于理解"他""那个""上面"等指代，
   **不是本次问题的证据**。回答只能依据【资料】；历史与资料冲突时以资料为准。
   历史里出现的 [资料N] 编号已失效，不要引用。"""

# 保留旧的常量名，避免外部引用断裂
HISTORY_TURNS = memory.HISTORY_TURNS


async def _history(session_id: str) -> list[dict]:
    """获取会话历史（完整回合，按 seq 排序）。"""
    return await memory.load_turns(session_id)


def _irrelevant_result(question: str, reply: str):
    """构造一个"无关 query"专用的空检索结果。

    复用 RetrievalResult 契约，让 chat 路由的 meta 事件照常渲染（mode=irrelevant、
    无 citations、degraded=True），reply 放在 detail 里回传。不碰数据库、不检索。
    """
    from app.services.retrieval.hybrid import RetrievalResult

    return RetrievalResult(
        query=question,
        mode="irrelevant",
        degraded=True,
        detail={
            "intent": "irrelevant",
            "reply": reply,
            "skipped": ["retrieval", "generation"],
        },
    )


def _expand_query(question: str, entities: list[str]) -> list[str]:
    """用上下文实体补全当前问句，产出额外的检索 query。

    只在"指代型"提问时启用："他后来怎么了""那他的师父呢"这类问句本身
    没有可检索的实体，原样丢给检索器只能靠泛化语义硬凑。
    把历史里的实体拼成补充 query 后，词法通道能精确命中。

    独立问题（"曹操和刘备是什么关系"）**不继承**实体：多塞一个不相干的
    实体进检索词只会稀释主题，拉低召回。

    返回的是**额外**的 query，调用方要跟原 query 一起召回 ——
    增强是加分项，不是替换项，这样即使继承的实体不相关，
    原问句那一路仍然能把正确答案捞回来。
    """
    if not entities:
        return []
    if not memory._is_anaphoric(question):
        return []
    cleaned = memory._strip_anaphora(question)
    if not cleaned:
        # 整句都是"他""那个"这类 → 实体继承价值最大，直接用实体做 query
        cleaned = " ".join(entities)
    else:
        cleaned = f"{' '.join(entities)} {cleaned}"
    return [cleaned]


async def _graph_facts(novel_id: str, question: str, extra_entities: list[str] | None = None) -> list[dict]:
    """问题里点名的实体 → 它们的一跳关系。

    实体匹配用"名字或别名出现在问题里"，而不是向量相似：
    实体名是短专名（2-4 字），向量对短文本的区分度很差，
    而精确子串匹配在这个场景下既快又准，也不会把"曹操"匹配到"曹操作风"以外的噪声。

    `extra_entities` 是从历史继承来的实体：多轮追问里"他的师父是谁"的问题本身
    不含人名，靠这一路才能定位到人。

    这条路径是**可缺席**的：没建图谱、没命中实体、查询出错，都只是少一段提示，
    不阻塞问答。
    """
    if not settings.graph_enabled:
        return []
    try:
        async with SessionLocal() as db:
            rows = (
                await db.execute(select(Entity.name, Entity.type, Entity.aliases).where(Entity.novel_id == novel_id))
            ).all()
            if not rows:
                return []
            hits: list[str] = []
            for name, etype, aliases in rows:
                # 只拿专名类实体去匹配：Location/Item 的名字常是普通词（"桌上"、"门外"），
                # 出现在问题里不代表用户想问它
                if etype not in ("Character", "Faction", "Event"):
                    continue
                if name and name in question:
                    hits.append(name)
                    continue
                for a in aliases or []:
                    if isinstance(a, str) and len(a) >= 2 and a in question:
                        hits.append(name)
                        break
            # 历史继承的实体：只有确实是本书记錄里的实体才用，
            # 否则用户随口一句"曹操"在别的书里不存在，就会引入幻觉
            known = {name for name, _t, _a in rows}
            for e in extra_entities or []:
                if e in known and e not in hits:
                    hits.append(e)
            if not hits:
                return []
            return await graph_store.facts_for_entities(db, novel_id, hits[:6])
    except Exception:  # noqa: BLE001
        logger.warning("图谱事实查询失败 novel=%s", novel_id, exc_info=True)
        return []


async def prepare(
    novel_id: str, question: str, session_id: str | None, *, use_graph: bool = True
) -> tuple[list[dict], object, str, list[dict], list[str], int]:
    """返回 (messages, retrieval_result, session_id, graph_facts, ctx_entities, turn_count)。

    开头先过一遍意图路由：纯闲聊 / 无内容 / 明确离题的短句会被判 IRRELEVANT，
    直接跳过检索与 LLM 生成，省一次召回 + 一次大模型调用。详见 `intent.py`。
    """
    intent, reply = classify(question)
    if intent is Intent.IRRELEVANT:
        logger.info("意图路由：无关 query，跳过检索与生成 q=%r", question[:30])
        # 兼容 RetrievalResult 契约：mode 标 irrelevant、无 citations，
        # reply 放在 detail 里让 chat 路由取走去当答案。
        result = _irrelevant_result(question, reply)
        return [], result, session_id or "", [], [], 0

    novel_title = ""
    async with SessionLocal() as db:
        novel = await db.get(Novel, novel_id)
        novel_title = novel.title if novel else ""

    turns = await memory.load_turns(session_id) if session_id else []
    # 已被摘要覆盖的回合不再重复塞进 prompt：摘要 + 这些原文同时出现，
    # 模型会当成两段不同的历史，反而更容易把细节说岔。
    summary, covered = await memory.load_summary(session_id) if session_id else ("", 0)
    if covered:
        turns = [t for t in turns if int(t.get("seq") or 0) > covered]

    # ---- 上下文实体 → 检索 query 增强 ----
    ctx_entities: list[str] = []
    if turns:
        ctx_entities = await memory.context_entities(novel_id, session_id)
    extra_queries = _expand_query(question, ctx_entities)

    result = await retrieve(novel_id, question, extra_queries=extra_queries)
    context = build_context(result.citations)
    facts = await _graph_facts(novel_id, question, extra_entities=ctx_entities) if use_graph else []

    parts: list[str] = []
    if context:
        parts.append(f"【资料】\n{context}")
    else:
        parts.append("【资料】\n（空：这本书尚未完成向量化，或没有检索到相关内容）")
    if facts:
        # 最多 20 条：再多会稀释资料、也压不住模型的"补充常识"倾向
        lines = "\n".join(f"- {f['text']}（第{f['chapter_no']}章）" for f in facts[:20])
        parts.append(f"【人物关系】（图谱抽取，仅辅助理清关系）\n{lines}")
    memory_block = memory.build_memory_block(turns, summary)
    if memory_block:
        parts.append(memory_block)
    parts.append(f"【问题】{question}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"（当前小说：《{novel_title}》）\n" + "\n\n".join(parts)},
    ]
    # turns 是消息列表（user + assistant 各一条），回合数要除以 2
    turn_count = len(turns) // 2
    return messages, result, session_id or "", facts, ctx_entities, turn_count


async def ensure_session(novel_id: str, session_id: str | None, question: str) -> str:
    from app.models import ChatSession

    if session_id:
        async with SessionLocal() as db:
            exists = await db.get(ChatSession, session_id)
            if exists is not None:
                return session_id
            # 会话不存在（前端传了脏 id 或会话已被删）→ 新建，而不是把消息写进空 session
            logger.info("会话 %s 不存在，新建", session_id)
    async with SessionLocal() as db:
        s = ChatSession(novel_id=novel_id, title=question[:30] or "新对话")
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s.id


async def stream_answer(messages: list[dict]) -> AsyncIterator[str]:
    llm = get_llm()
    async for delta in llm.stream(messages, temperature=0.6, max_tokens=4096):
        yield delta
