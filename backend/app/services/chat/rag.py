"""RAG 问答：检索 → 组装 prompt → 流式生成 → 带引用落库。"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator

from sqlalchemy import desc, select

from app.db import SessionLocal
from app.models import ChatMessage, ChatSession, Novel
from app.services.llm.client import get_llm
from app.services.retrieval.hybrid import build_context, retrieve

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一位严谨的小说内容助手。请严格依据【资料】回答用户问题。

规则：
1. 只使用【资料】中的信息。资料没有提到的事实，直接说明"资料中未提及"，不要推测。
2. 回答末尾用 [资料N] 标注依据；多个来源写成 [资料1][资料3]。
3. 涉及人物、时间、地点的细节必须与原文一致，不要改写专有名词。
4. 用中文回答，条理清晰，一般不超过 400 字；用户要求详细时再展开。
5. 如果用户问的是"这本书讲了什么"这类总览问题，按章节顺序概括，不要编造结局。"""

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


async def prepare(novel_id: str, question: str, session_id: str | None) -> tuple[list[dict], object, str]:
    """返回 (messages, retrieval_result, session_id)。"""
    novel_title = ""
    async with SessionLocal() as db:
        novel = await db.get(Novel, novel_id)
        novel_title = novel.title if novel else ""

    result = await retrieve(novel_id, question)
    context = build_context(result.citations)
    history = await _history(session_id) if session_id else []

    if not context:
        user_block = f"【资料】\n（空：这本书尚未完成向量化，或没有检索到相关内容）\n\n【问题】{question}"
    else:
        user_block = f"【资料】\n{context}\n\n【问题】{question}"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        *history,
        {"role": "user", "content": f"（当前小说：《{novel_title}》）\n{user_block}"},
    ]
    return messages, result, session_id or ""


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
    async for delta in llm.stream(messages, temperature=0.6, max_tokens=3000):
        yield delta
