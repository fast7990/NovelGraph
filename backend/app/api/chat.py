"""RAG 问答：SSE 流式。"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime
from urllib.parse import quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from sqlalchemy import delete, desc, func, or_, select

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
    session_id: str,
    qseq: int,
    message_id: str,
    answer: str,
    cites: list[dict],
) -> dict:
    """存回答 → 结掉这一轮的 token 账 → 视情况压缩早期回合。

    三件事绑在一起，是因为它们都以"这一轮已落库"为前提：摘要要能看到最新消息，
    用量要包含这一轮的请求。任一步失败都不该连累另外两步，所以各自兜住。

    `message_id` 是开流前 `create_answer()` 预建的那条空消息；它可能为空
    （预建失败），那时退化回"这轮结束时新建一条"，不能因为一次插入失败
    就把回答丢掉。

    返回 `{"round": 本轮用量, "total": 会话累计, "message_id": 消息 id}`。
    """
    round_usage: dict = {}
    try:
        if message_id:
            await memory.fill_answer(message_id, answer, cites)
        else:
            message_id = await memory.save_answer(session_id, qseq, answer, cites)
    except Exception:  # noqa: BLE001
        logger.exception("落库失败 会话=%s", session_id)
    try:
        round_usage = await memory.flush_usage(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("用量落账失败 会话=%s", session_id, exc_info=True)
    try:
        await memory.maybe_summarize(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("会话摘要调度失败 会话=%s", session_id, exc_info=True)
    try:
        total = await memory.session_usage(session_id)
    except Exception:  # noqa: BLE001
        logger.warning("读累计用量失败 会话=%s", session_id, exc_info=True)
        total = {}
    return {"round": round_usage, "total": total, "message_id": message_id}


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
        "chat 会话=%s 入参session=%s 重新生成=%s 问题=%r",
        session_id,
        req.session_id,
        req.regenerate,
        req.question[:30],
    )
    # 重新生成要在 `prepare()` **之前**删掉上一轮：prepare 会读历史做上下文消歧，
    # 不先删的话，要重答的这一轮会被当成"上文"塞进 prompt 和检索，
    # 等于让模型参考着自己上一版答案再答一遍。
    if req.regenerate:
        dropped = await memory.drop_last_turn(session_id, expect=req.question)
        if dropped:
            logger.info("重新生成：已删除上一轮问答 会话=%s", session_id)
        else:
            logger.info("重新生成：没有可删的上一轮，按普通提问处理 会话=%s", session_id)
    messages, result, _, graph_facts, ctx_entities, turns = await rag.prepare(
        req.novel_id, req.question, session_id, use_graph=req.use_graph
    )
    # 无关 query（问候 / 离题短句）：intent.py 已跳过检索与生成，这里直接取回话术
    canned_reply = result.detail.get("reply") if result.mode == "irrelevant" else None
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
        # 答案的 id 也在开流前就建好。前端拿到它之后 UI 里就只有一套 id，
        # 答完不用再换 —— 换 id 会让 Semi 的 key 变掉、正文重挂载闪一下。
        answer_mid = ""
        try:
            answer_mid = await memory.create_answer(session_id, qseq)
        except Exception:  # noqa: BLE001
            logger.exception("预建回答消息失败 会话=%s，退化到结束时新建", session_id)

        yield _sse(
            "meta",
            {
                "session_id": session_id,
                # 真实的消息 id。前端发送时手里只有本地临时 `a-<ts>-<rand>`，
                # 靠这个在**正文还没开始**的时候换成服务端 id：
                # 换成之后 "点赞 / 删除这条答案" 有真对象可指，也不会因为
                # key 变化把已经流出来的正文重新挂载一次。
                "message_id": answer_mid,
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
            task = asyncio.create_task(
                _persist_and_housekeep(session_id, qseq, answer_mid, answer, cites)
            )

            def _done(t: asyncio.Task) -> None:
                if t.cancelled():
                    return
                exc = t.exception()
                if exc is not None:  # noqa: BLE001
                    logger.error("落库失败 会话=%s: %s", session_id, exc)

            task.add_done_callback(_done)

        finished = False
        if canned_reply:
            # 无关 query：跳过检索与 LLM 生成，直接回礼貌话术（省一次召回 + 一次大模型调用）
            ttft_ms = elapsed()
            buffer.append(canned_reply)
            yield _sse("delta", {"text": canned_reply})
            answer = canned_reply
            finished = True
        else:
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
        usage_info: dict = {}
        if answer.strip():
            usage_info = await _persist_and_housekeep(session_id, qseq, answer_mid, answer, citations)
            saved = True
        else:
            # 空回答也要把用量与摘要推进掉，否则这一轮的 token 和压缩机会都白丢
            await memory.flush_usage(session_id)
            # 预建的空壳必须删掉：留着会在回放里变成一个空气泡，
            # 还会让摘要按"多了一轮回答"去配对
            if answer_mid:
                try:
                    await memory.fill_answer(answer_mid, "", [])
                except Exception:  # noqa: BLE001
                    logger.warning("清理空回答失败 id=%s", answer_mid, exc_info=True)
            usage_info = {
                "round": {},
                "total": await memory.session_usage(session_id),
                "message_id": answer_mid,
            }
        logger.info(
            "chat 落库 会话=%s 回答长度=%d 引用=%d token=%s",
            session_id,
            len(answer),
            len(citations),
            usage_info.get("round") or "-",
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
                # 同一个 id 在 meta 里已经下发过，这里再带一次是兜底：
                # 万一前端是从中途接上来的（预建失败、或换了客户端），
                # 靠它仍然能把这条答案对上真实记录
                "message_id": usage_info.get("message_id") or answer_mid,
                # 本轮的 token 消耗与会话累计：前端在答案下方直接显示，
                # 用户不用去侧栏找、也不用等列表刷新。
                # 只挑这两个子字段，别把 message_id 混进用量结构里
                "usage": {
                    "round": usage_info.get("round") or {},
                    "total": usage_info.get("total") or {},
                },
            },
        )

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/sessions")
async def list_sessions(novel_id: str, q: str = "") -> dict:
    """列出某本书的会话，按最近活跃排序。

    带上 `message_count` 与 `last_message_at`：前端要显示"这个会话有几句、
    最后聊到什么时候"，否则一排会话标题长得都一样（都是首句截断），
    用户分不清哪个是自己要回的那个。

    `q` 非空时按**标题或消息正文**过滤。只搜标题不够用：标题是首句截断，
    用户记得的往往是"我问过 XX 那件事"，而 XX 可能出现在第三轮。
    命中正文时额外返回 `hit`（首个命中的提问片段），让用户知道为什么这条被搜出来。
    """
    q = (q or "").strip()
    async with SessionLocal() as db:
        stmt = (
            select(
                ChatSession,
                func.count(ChatMessage.id).label("message_count"),
                func.max(ChatMessage.created_at).label("last_message_at"),
            )
            .outerjoin(ChatMessage, ChatMessage.session_id == ChatSession.id)
            .where(ChatSession.novel_id == novel_id)
        )
        if q:
            like = f"%{q}%"
            hit_sessions = select(ChatMessage.session_id).where(ChatMessage.content.ilike(like))
            stmt = stmt.where(
                or_(ChatSession.title.ilike(like), ChatSession.id.in_(hit_sessions))
            )
        rows = (
            await db.execute(
                stmt.group_by(ChatSession.id)
                .order_by(desc(func.coalesce(func.max(ChatMessage.created_at), ChatSession.updated_at)))
                .limit(100)
            )
        ).all()

        # 命中片段：每条会话取**最早**的一条命中消息（提问优先于答案，
        # 因为"我问过什么"比"它答了什么"更能帮用户认出这是哪个对话）
        hits: dict[str, str] = {}
        if q and rows:
            ids = [s.id for s, _, _ in rows]
            hit_rows = (
                await db.execute(
                    select(ChatMessage.session_id, ChatMessage.content, ChatMessage.role)
                    .where(ChatMessage.session_id.in_(ids), ChatMessage.content.ilike(f"%{q}%"))
                    # role 按字母倒序：'user' > 'assistant'，提问排在答案前面。
                    # 这样 setdefault 取到的第一条命中就是提问（用户更容易认出"我问过什么"）
                    .order_by(ChatMessage.session_id, ChatMessage.role.desc(), ChatMessage.seq)
                )
            ).all()
            for sid, content, _role in hit_rows:
                hits.setdefault(sid, content or "")

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
                # 搜索命中片段（未搜索时为空串）
                "hit": _snippet(hits.get(s.id, ""), q) if q else "",
            }
            for s, count, last_at in rows
        ]
    }


def _snippet(text: str, q: str, width: int = 28) -> str:
    """把命中词前后各截一段，避免把整条消息搬到列表里。"""
    if not text:
        return ""
    pos = text.lower().find(q.lower()) if q else -1
    if pos < 0:
        return text[: width * 2]
    start = max(0, pos - width)
    end = min(len(text), pos + len(q) + width)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"



@router.get("/chat/sessions/{session_id}/messages")
async def session_messages(session_id: str) -> dict:
    """回放一个会话的完整消息。

    按 `seq` 排序：同一回合的 user/assistant 可能落在同一微秒
    （同一事务两次 insert），只按 created_at 排会偶发把 assistant 排到前面，
    回放出来的对话是倒的。

    顺带返回会话的累计用量：重新打开一个旧对话时，气泡里的"本轮消耗"是历史
    数据、按消息没存，但"这个会话一共花了多少"是有的。不带的话用户刷新一次
    就以为台账没了。
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
                "feedback": r.feedback or "",
                "created_at": r.created_at,
            }
            for r in rows
        ],
        "usage": await memory.session_usage(session_id),
    }


@router.get("/chat/sessions/{session_id}/export")
async def export_session(session_id: str) -> Response:
    """把一个会话导出成 Markdown 附件。

    为什么是 Markdown 而不是 JSON：用户导出对话是为了**带走结论**（贴进笔记、
    发给别人、存档），不是做数据备份。JSON 对他没用，而 Markdown 里引用原文、
    章节号、token 消耗都能顺手带上。

    文件名用 `filename*=UTF-8''` 而不是直接塞中文：RFC 5987 规定的写法，
    `filename=` 里出现非 ASCII 会让部分浏览器把文件名变成乱码或直接截断。
    """
    async with SessionLocal() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            raise HTTPException(404, "会话不存在")
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.seq, ChatMessage.id)
            )
        ).scalars().all()
        usage = await memory.session_usage(session_id)

    title = (sess.title or "对话").strip() or "对话"
    lines = [f"# {title}", ""]
    meta = [
        f"共 {len(rows)} 条消息",
        f"导出时间 {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')} UTC",
    ]
    if usage.get("total_tokens"):
        meta.append(
            f"消耗 {usage['total_tokens']} tok"
            f"（入 {usage.get('prompt_tokens', 0)} / 出 {usage.get('completion_tokens', 0)}）"
        )
    if usage.get("summary_chars"):
        meta.append(f"早期 {int(usage.get('summary_seq', 0)) // 2} 轮已压缩为摘要")
    lines += ["> " + " · ".join(meta), ""]

    for m in rows:
        who = "我" if m.role == "user" else "小说助手"
        lines += [f"### {who}", "", (m.content or "").strip(), ""]
        for i, c in enumerate(m.citations or [], 1):
            score = c.get("score")
            score_txt = f" · {float(score):.3f}" if isinstance(score, (int, float)) else ""
            lines.append(f"> [{i}] 第 {c.get('chapter_no', '?')} 章{score_txt}")
            lines.append(f"> {c.get('snippet', '')}")
            lines.append("")
    body = "\n".join(lines).rstrip() + "\n"

    filename = quote(f"{title}.md")
    return Response(
        content=body,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )


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


@router.patch("/chat/messages/{message_id}/feedback")
async def set_message_feedback(message_id: str, payload: dict) -> dict:
    """给一条回答打「有用 / 没用」。传空字符串表示取消。

    `feedback` 只有一列而不是 like/dislike 两个布尔：赞和踩是互斥的，
    两列会允许 `like=1, dislike=1` 这种非法态，出问题时无从判断以谁为准。
    单列 + 枚举让状态机只有一个真相来源。踩的理由将来要收集，也是加
    `feedback_note` 列，而不是再加一个布尔。
    """
    value = str(payload.get("feedback", "")).strip().lower()
    if value not in ("", "up", "down"):
        raise HTTPException(400, "feedback 只能是 up / down / 空")
    async with SessionLocal() as db:
        msg = await db.get(ChatMessage, message_id)
        if msg is None:
            raise HTTPException(404, "消息不存在")
        if msg.role != "assistant":
            raise HTTPException(400, "只能对回答打反馈")
        msg.feedback = value
        session_id = msg.session_id
        await db.commit()
    return {"id": message_id, "feedback": value, "session_id": session_id}


@router.delete("/chat/messages/{message_id}")
async def delete_message(message_id: str) -> dict:
    """删除一条消息。

    只删点中的那一条，不连带删配对的另一条：界面上的垃圾桶就长在这条消息
    底下，用户对它的预期是"这个消失"。如果偷偷把上面的提问也删掉，反而
    制造了"我删的不是我点的那条"的意外。
    """
    async with SessionLocal() as db:
        msg = await db.get(ChatMessage, message_id)
        if msg is None:
            raise HTTPException(404, "消息不存在")
        session_id = msg.session_id
        await db.delete(msg)
        await db.commit()
        # 删空了就把会话也收掉：否则侧栏留一个"0 条"的幽灵会话，
        # 点进去什么也没有，用户还得再删一次
        left = await db.scalar(
            select(func.count()).select_from(ChatMessage).where(ChatMessage.session_id == session_id)
        )
        if not left:
            sess = await db.get(ChatSession, session_id)
            if sess is not None:
                await db.delete(sess)
                await db.commit()
    return {"deleted": message_id, "session_id": session_id, "session_deleted": not left}


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
