"""RAG 问答：检索 → 组装 prompt → 流式生成 → 带引用落库。

P1 起多了一路**图谱事实**：问题里提到的实体，把它的一跳关系作为短句注入 prompt。
和向量检索是两条互补的路——向量找的是"这段话在哪"，图谱给的是"这两个人是什么关系"。
后者对"谁和谁是什么关系""某某的师父是谁"这类问题，比再多召回十条切片都直接。
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import desc, select

from app.config import settings
from app.db import SessionLocal
from app.models import ChatMessage, ChatSession, Entity, Novel
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
   人物关系里没写的内容不要当作事实补充进答案。"""

HISTORY_TURNS = 6


async def _history(session_id: str) -> list[dict]:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(desc(ChatMessage.created_at))
                .limit(HISTORY_TURNS * 2)
            )
        ).scalars().all()
    rows = list(reversed(rows))
    return [{"role": r.role, "content": r.content} for r in rows if r.content]


async def _graph_facts(novel_id: str, question: str) -> list[dict]:
    """问题里点名的实体 → 它们的一跳关系。

    实体匹配用"名字或别名出现在问题里"，而不是向量相似：
    实体名是短专名（2-4 字），向量对短文本的区分度很差，
    而精确子串匹配在这个场景下既快又准，也不会把"曹操"匹配到"曹操作风"以外的噪声。

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
            if not hits:
                return []
            return await graph_store.facts_for_entities(db, novel_id, hits[:6])
    except Exception:  # noqa: BLE001
        logger.warning("图谱事实查询失败 novel=%s", novel_id, exc_info=True)
        return []


async def prepare(
    novel_id: str, question: str, session_id: str | None, *, use_graph: bool = True
) -> tuple[list[dict], object, str, list[dict]]:
    """返回 (messages, retrieval_result, session_id, graph_facts)。"""
    novel_title = ""
    async with SessionLocal() as db:
        novel = await db.get(Novel, novel_id)
        novel_title = novel.title if novel else ""

    result = await retrieve(novel_id, question)
    context = build_context(result.citations)
    history = await _history(session_id) if session_id else []
    facts = await _graph_facts(novel_id, question) if use_graph else []

    parts: list[str] = []
    if context:
        parts.append(f"【资料】\n{context}")
    else:
        parts.append("【资料】\n（空：这本书尚未完成向量化，或没有检索到相关内容）")
    if facts:
        # 最多 20 条：再多会稀释资料、也压不住模型的"补充常识"倾向
        lines = "\n".join(f"- {f['text']}（第{f['chapter_no']}章）" for f in facts[:20])
        parts.append(f"【人物关系】（图谱抽取，仅辅助理清关系）\n{lines}")
    parts.append(f"【问题】{question}")

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": f"（当前小说：《{novel_title}》）\n" + "\n\n".join(parts)},
    ]
    return messages, result, session_id or "", facts


async def ensure_session(novel_id: str, session_id: str | None, question: str) -> str:
    if session_id:
        return session_id
    async with SessionLocal() as db:
        s = ChatSession(novel_id=novel_id, title=question[:30] or "新对话")
        db.add(s)
        await db.commit()
        await db.refresh(s)
        return s.id


async def save_turn(session_id: str, question: str, answer: str, citations: list[dict]) -> None:
    async with SessionLocal() as db:
        db.add(ChatMessage(session_id=session_id, role="user", content=question))
        db.add(ChatMessage(session_id=session_id, role="assistant", content=answer, citations=citations))
        await db.commit()


async def stream_answer(messages: list[dict]) -> AsyncIterator[str]:
    llm = get_llm()
    async for delta in llm.stream(messages, temperature=0.6, max_tokens=4096):
        yield delta
