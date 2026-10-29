"""RAG 问答：SSE 流式。"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from sqlalchemy import delete, desc, func, select

from app.config import settings
from app.db import SessionLocal
from app.models import ChatMessage, ChatSession, Novel
from app.schemas import ChatRequest
from app.services.chat import memory, rag
from app.services.llm.client import get_llm
from app.services.retrieval.hybrid import retrieve

logger = logging.getLogger(__name__)
router = APIRouter(tags=["chat"])


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def _persist_and_housekeep(
    session_id: str, qseq: int, answer: str, cites: list[dict]
) -> None:
    """存回答 → 结掉这一轮的 token 账 → 视情况压缩早期回合。

    三件事绑在一起，是因为它们都以"这一轮已落库"为前提：摘要要能看到最新消息，
    用量要包含这一轮的请求。任一步失败都不该连累另外两步，所以各自兜住。
    """
    try:
        await memory.save_answer(session_id, qseq, answer, cites)
    except Exception:  # noqa: BLE001
        logger.exception("落库失败 会话=%s", session_id)
    try:
        await memory.flush_usage(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("用量落账失败 会话=%s", session_id, exc_info=True)
    try:
        await memory.maybe_summarize(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("会话摘要调度失败 会话=%s", session_id, exc_info=True)


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
    logger.info(
        "chat 会话=%s 入参session=%s 问题=%r",
        session_id,
        req.session_id,
        req.question[:30],
    )
    messages, result, _, graph_facts, ctx_entities, turns = await rag.prepare(
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

        # 先落提问：客户端可能在生成中途刷新/关页，那时服务端生成器会被取消，
        # 走不到后面的落库代码。先存这一半，至少"问过什么"不会丢。
        qseq = await memory.save_question(session_id, req.question)

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
                # 本轮用到的历史上下文：前端在答案上方显示"基于上文 X"，
                # 让用户能确认模型确实接住了上下文，而不是靠猜。
                "memory": {
                    "turns": turns,
                    "entities": ctx_entities,
                    "expanded_queries": result.detail.get("queries", [])[1:],
                },
            },
        )
        buffer: list[str] = []
        reasoning_chars = 0
        ttft_ms: int | None = None
        reasoning_ms: int | None = None
        saved = False

        def persist(answer: str, cites: list[dict]) -> None:
            """后台任务落库。

            不能用 await：客户端断开时生成器要么收到 CancelledError、要么被
            `aclose()` 关闭（GeneratorExit），这两种情况下在 finally 里 await
            都会被立刻打断或直接抛 RuntimeError。用独立 task 让它跑完。
            """
            nonlocal saved
            if saved or not answer.strip():
                return
            saved = True
            task = asyncio.create_task(_persist_and_housekeep(session_id, qseq, answer, cites))

            def _done(t: asyncio.Task) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:  # noqa: BLE001
                    logger.error("落库失败 会话=%s: %s", session_id, exc)

            task.add_done_callback(_done)

        finished = False
        try:
            async for kind, text in llm.stream_events(
                messages,
                temperature=0.6,
                max_tokens=settings.chat_max_tokens,
                # 用会话 id 做计量 tag：流式请求不主动要 usage 的话，网关不会回，
                # 问答的 token 消耗就永远是 0
                usage_tag=session_id,
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
            finished = True
        except Exception as exc:  # noqa: BLE001
            logger.exception("生成失败")
            persist("".join(buffer) or f"（生成失败：{type(exc).__name__}）", citations)
            yield _sse("error", {"message": f"生成失败：{type(exc).__name__}: {exc}"})
            return
        finally:
            # 只有"没跑完"才走这里（CancelledError / GeneratorExit）：客户端刷新或
            # 关页时生成器被取消，此时存下已生成的部分并标注中断，用户回来能看个半截，
            # 而不是整轮凭空消失。
            if not finished and not saved:
                persist("".join(buffer) or "（本轮生成被中断）", citations)

        answer = "".join(buffer)
        if answer.strip():
            await _persist_and_housekeep(session_id, qseq, answer, citations)
            saved = True
        else:
            # 空回答也要把用量与摘要推进掉，否则这一轮的 token 和压缩机会都白丢
            await memory.flush_usage(session_id)
        logger.info(
            "chat 落库 会话=%s 回答长度=%d 引用=%d", session_id, len(answer), len(citations)
        )
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
    """列出某本书的会话，按最近活跃排序。

    带上 `message_count` 与 `last_message_at`：前端要显示"这个会话有几句、
    最后聊到什么时候"，否则一排会话标题长得都一样（都是首句截断），
    用户分不清哪个是自己要回的那个。
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(
                    ChatSession,
                    func.count(ChatMessage.id).label("message_count"),
                    func.max(ChatMessage.created_at).label("last_message_at"),
                )
                .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
                .where(ChatSession.novel_id == novel_id)
                .group_by(ChatSession.id)
                .order_by(desc(func.coalesce(func.max(ChatMessage.created_at), ChatSession.updated_at)))
                .limit(100)
            )
        ).all()
    return {
        "items": [
            {
                "id": s.id,
                "title": s.title,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
                "message_count": int(count or 0),
                "last_message_at": last_at,
                # 会话成本台账：问答的 token 消耗落在这里（图谱构建记在 job.stats 上）
                "prompt_tokens": int(s.prompt_tokens or 0),
                "completion_tokens": int(s.completion_tokens or 0),
                "llm_calls": int(s.llm_calls or 0),
                # 长对话是否已被压缩过：前端据此提示"早期内容已折叠成摘要"
                "summary_chars": len(s.summary or ""),
                "summary_seq": int(s.summary_seq or 0),
            }
            for s, count, last_at in rows
        ]
    }


@router.get("/chat/sessions/{session_id}/messages")
async def session_messages(session_id: str) -> dict:
    """回放一个会话的完整消息。

    按 `seq` 排序：同一回合的 user/assistant 可能落在同一微秒
    （同一事务两次 insert），只按 created_at 排会偶发把 assistant 排到前面，
    回放出来的对话是倒的。
    """
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.seq, ChatMessage.id)
            )
        ).scalars().all()
    return {
        "items": [
            {
                "id": r.id,
                "role": r.role,
                "content": r.content,
                "citations": r.citations,
                "seq": r.seq,
                "created_at": r.created_at,
            }
            for r in rows
        ]
    }


@router.patch("/chat/sessions/{session_id}")
async def rename_session(session_id: str, payload: dict) -> dict:
    """重命名会话。标题为空则回退到"新对话"。"""
    title = str(payload.get("title", "")).strip()[:100]
    if not title:
        raise HTTPException(400, "标题不能为空")
    async with SessionLocal() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            raise HTTPException(404, "会话不存在")
        sess.title = title
        await db.commit()
    return {"id": session_id, "title": title}


@router.delete("/chat/sessions/{session_id}")
async def delete_session(session_id: str) -> dict:
    """删除会话及其消息。

    消息必须一起删：`chat_messages` 没有外键指向 `chat_sessions`，
    只删会话会留下孤儿消息，之后同一个 session_id 被复用时
    会把旧消息一起拉出来。
    """
    async with SessionLocal() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            raise HTTPException(404, "会话不存在")
        await db.execute(delete(ChatMessage).where(ChatMessage.session_id == session_id))
        await db.delete(sess)
        await db.commit()
    return {"deleted": session_id}


@router.post("/chat/sessions")
async def create_session(novel_id: str) -> dict:
    """显式新建一个空会话。

    前端点"新对话"时立刻建一个，不等用户发第一条消息 ——
    这样刷新页面能恢复到那个空会话，而不是静默开一个新的。
    """
    async with SessionLocal() as db:
        novel = await db.get(Novel, novel_id)
        if novel is None:
            raise HTTPException(404, "小说不存在")
        s = ChatSession(novel_id=novel_id, title="新对话")
        db.add(s)
        await db.commit()
        await db.refresh(s)
    return {"id": s.id, "title": s.title, "created_at": s.created_at, "updated_at": s.updated_at}


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
