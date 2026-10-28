"""RAG 问答：SSE 流式。"""

from __future__ import annotations

import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import desc, select

from app.config import settings
from app.db import SessionLocal
from app.models import ChatMessage, ChatSession, Novel
from app.schemas import ChatRequest
from app.services.chat import rag
from app.services.llm.client import get_llm
from app.services.retrieval.hybrid import retrieve

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


@router.post("/chat")
async def chat(req: ChatRequest, request: Request) -> StreamingResponse:
    if not req.question.strip():
        raise HTTPException(400, "问题不能为空")

    async with SessionLocal() as db:
        novel = await db.get(Novel, req.novel_id)
    if not novel:
        raise HTTPException(404, "小说不存在")
    if novel.status not in ("READY", "VECTORIZING"):
        raise HTTPException(409, f"小说尚未完成入库（当前状态 {novel.status}），请等待切片与向量化完成")

    session_id = await rag.ensure_session(req.novel_id, req.session_id, req.question)
    messages, result, _, graph_facts = await rag.prepare(
        req.novel_id, req.question, session_id, use_graph=req.use_graph
    )
    citations = [c.to_dict() for c in result.citations]
    llm = get_llm()
    show_reasoning = settings.llm_show_reasoning

    async def gen():
        t0 = time.perf_counter()

        def elapsed() -> int:
            """距本轮生成开始的毫秒数（不含检索：gen() 是在 prepare 之后才进的）。"""
            return int((time.perf_counter() - t0) * 1000)

        yield _sse(
            "meta",
            {
                "session_id": session_id,
                "citations": citations,
                "degraded": result.degraded,
                "mode": result.mode,
                "retrieval": result.detail,
                "graph_facts": graph_facts,
                # 诊断用：关掉思考推送（LLM_SHOW_REASONING=false）时前端据此知道
                # "不是模型没想，是没让你看"，排查时不用怀疑是链路丢了事件
                "show_reasoning": show_reasoning,
            },
        )
        buffer: list[str] = []
        reasoning_chars = 0
        ttft_ms: int | None = None
        reasoning_ms: int | None = None
        try:
            async for kind, text in llm.stream_events(
                messages, temperature=0.6, max_tokens=settings.chat_max_tokens
            ):
                if await request.is_disconnected():
                    logger.info("客户端断开，中止生成")
                    break
                if kind == "reasoning":
                    reasoning_chars += len(text)
                    if show_reasoning:
                        # 事件里带 elapsed_ms：前端不用自己数秒，重连/慢网下也不会漂
                        yield _sse("reasoning", {"text": text, "elapsed_ms": elapsed()})
                    continue
                if ttft_ms is None:
                    ttft_ms = elapsed()
                    if reasoning_chars:
                        reasoning_ms = ttft_ms
                buffer.append(text)
                yield _sse("delta", {"text": text})
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成失败")
            yield _sse("error", {"message": f"生成失败：{type(exc).__name__}: {exc}"})
            return

        answer = "".join(buffer)
        if answer.strip():
            await rag.save_turn(session_id, req.question, answer, citations)
        yield _sse(
            "done",
            {
                "session_id": session_id,
                "chars": len(answer),
                "reasoning_chars": reasoning_chars,
                "reasoning_ms": reasoning_ms,
                "ttft_ms": ttft_ms,
                "elapsed_ms": elapsed(),
            },
        )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/sessions")
async def list_sessions(novel_id: str) -> dict:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatSession).where(ChatSession.novel_id == novel_id).order_by(desc(ChatSession.created_at)).limit(50)
            )
        ).scalars().all()
    return {"items": [{"id": s.id, "title": s.title, "created_at": s.created_at} for s in rows]}


@router.get("/chat/sessions/{session_id}/messages")
async def session_messages(session_id: str) -> dict:
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage).where(ChatMessage.session_id == session_id).order_by(ChatMessage.created_at)
            )
        ).scalars().all()
    return {
        "items": [
            {"id": r.id, "role": r.role, "content": r.content, "citations": r.citations, "created_at": r.created_at}
            for r in rows
        ]
    }


@router.post("/search")
async def debug_search(req: dict) -> dict:
    """检索调试接口：不生成答案，只看召回。

    返回里带 `mode`，可以直接看出走的是 hybrid / dense_only / sparse_only / empty，
    调参时不用猜是哪一路断了。
    """
    novel_id = req.get("novel_id", "")
    query = req.get("query", "")
    top_k = int(req.get("top_k", 10))
    if not novel_id or not query:
        raise HTTPException(400, "novel_id / query 必填")
    result = await retrieve(novel_id, query, top_k=top_k)
    return result.to_dict(with_content=True)
