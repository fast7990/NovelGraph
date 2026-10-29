"""会话记忆：落库、历史回放、上下文实体提取。

这一层解决的是"多轮对话里模型到底知道什么"。

**为什么历史不能只进 prompt**：RAG 的召回只吃当前 query。用户在第二轮问
"他后来怎么样了"，这个 query 里没有任何可检索的实体名，词法通道直接空，
向量通道只能靠"后来""怎么样"这种泛化语义硬凑 —— 实测召回的是第 79、120 回
的无关章节，分数 0.31，而同话题的完整问法能拿到 0.53 命中正确章节。

所以记忆要分两路用：
1. **进 prompt**：让模型知道上文说了什么（已有能力，本模块负责回放质量）；
2. **进检索**：把历史里出现的实体继承到当前 query，让召回有锚点。

第 2 ���是本模块的主要产出，也是"上下文记忆"和"多轮对话"的真正分界线。
"""

from __future__ import annotations

import logging
import re

from sqlalchemy import delete, desc, func, select, update

from app.config import settings
from app.db import SessionLocal
from app.models import ChatMessage, ChatSession

logger = logging.getLogger(__name__)

# 一次带进 prompt 的历史回合数上限。
# 每回合 = 1 条 user + 1 条 assistant，答案可能上千字，取 6 轮约 6-10K 字符，
# 加上检索资料 8K 仍在合理预算内。再多就挤掉检索上下文，得不偿失。
HISTORY_TURNS = 6

# 一次带进检索的上下文实体上限。太多会让 query 变成实体堆，反而稀释语义。
MAX_CONTEXT_ENTITIES = 3

# 单个实体的最大长度：超过 6 字的"实体"多半是句子片段，不是专名。
_MAX_ENTITY_LEN = 6

# 指代词/省略式提问的信号词。命中才做实体继承 —— 独立问题（如"曹操是谁"）
# 不需要继承，硬继承反而会引入无关实体。
_ANAPHORA = (
    "他", "她", "它", "他们", "她们", "此人",
    "那个", "这个", "那位", "这位", "前者", "后者", "上面", "上面提到",
    "刚才", "刚刚", "前面", "之前", "后来", "然后", "接着", "还有呢",
    # 纯指代句（"那呢""还有吗""怎么说"）—— 这些问句里没有可检索的内容，
    # 去掉指代词后为空，正好走"直接用实体做 query"这条分支
    "那呢", "还有吗", "怎么说", "为什么呢",
)

# 中文停用/泛义词：这些词在任何小说里都高频出现，不能当实体继承。
_STOPWORDS = {
    "小说", "主角", "故事", "情节", "内容", "人物", "角色", "关系",
    "作者", "本书", "全书", "章节", "这段", "那个", "这个",
    "问题", "答案", "资料", "根据", "提到", "介绍", "之间",
    "为什么", "怎么样", "如何", "什么", "谁", "哪里", "什么时候",
}

# 虚词/助词：专名几乎不会以这些字**结尾**。
# 这是过滤候选的主要手段 —— 枚举所有 2-6 字子串会产出
# "华佗的著""佗的""了刘备"这类垃圾，靠这条能砍掉九成。
_TAIL_STOP = set(
    "的了吧呢啊呀嘛么呀着过地得和与或及在对从为是被把就都也很更最又再才只"
    "上下中里外前后间些个么什谁呢这那其此某每各另同另"
    # 动词/形容/副词类尾巴：专名不会以这些字结尾
    "有作做叫说道问答想知见看来去来出入到过起住"
    # 疑问词尾巴
    "哪几多少"
)

# 专名几乎不会以这些字**开头**（它们是助词/连词）。
_HEAD_STOP = set(
    "的了着过地得和与或及在对从为是被把就都也很更最又再才只个么什"
    "这那其此某每各另同"
)


def _is_plausible_entity(cand: str) -> bool:
    """粗筛一个候选串是否像专名。"""
    if len(cand) < 2:
        return False
    if cand in _STOPWORDS:
        return False
    if cand[0] in _HEAD_STOP:
        return False
    if cand[-1] in _TAIL_STOP:
        return False
    # 含指代词的不是专名
    if any(w in cand for w in ("他们", "她们", "这个", "那个", "上述", "前面")):
        return False
    return True


def _is_anaphoric(question: str) -> bool:
    """判断问句是否依赖上文（指代型/省略型）。

    这是实体继承的**开关**：只有依赖上文的问句才需要借历史实体的力。
    独立问题（"曹操和刘备是什么关系"）如果也继承实体，query 会变成
    "华佗 曹操和刘备是什么关系"，把不相关的实体塞进检索词，
    反而拉低召回质量。
    """
    return any(w in question for w in _ANAPHORA)


def _strip_anaphora(text: str) -> str:
    """把指代词从问句里摘掉，只留下可检索的实体线索。

    "他后来怎么样了" → "后来怎么样了"
    """
    out = text
    for w in _ANAPHORA:
        out = out.replace(w, " ")
    return re.sub(r"\s+", " ", out).strip()


def _extract_entities(text: str) -> list[str]:
    """从一段中文里粗提专名候选。

    刻意不用模型/NER：这一步每轮都要跑，规则法的延迟是微秒级，
    而模型要多花 1-3 秒。规则法召回不全（漏掉生僻名），
    但漏召回只是"退化成原 query"，不会造成错答 —— 这个不对称性
    决定了它比模型抽取更划算。

    纯规则抽出来的候选必然带噪声（"华佗的著作有"里的"著作有"、
    "他后来在长"的"后来在"），所以这里只负责**产出候选**，
    最终是否采纳由 `_verified_entities()` 对着图谱实体表校验 ——
    候选必须在本书记载的实体名/别名里存在才作数。噪声自动归零，
    且顺带解决"用户随口说一个人名但这本书没这个人"的情况。
    """
    segments = re.split(r"[^一-龥]+", text)
    out: list[str] = []
    for seg in segments:
        if not seg:
            continue
        n = len(seg)
        for size in range(2, min(_MAX_ENTITY_LEN, n) + 1):
            for i in range(n - size + 1):
                c = seg[i : i + size]
                if not _is_plausible_entity(c):
                    continue
                if c not in out:
                    out.append(c)
    # 短的优先：短名更可能是完整专名；长的往往是短名+后缀的组合
    out.sort(key=len)
    return out


async def _verified_entities(novel_id: str, candidates: list[str]) -> list[str]:
    """把候选对着本图的实体表过一遍，只保留确实存在的实体。

    这是消噪的**决定性一步**：规则抽取必然带噪声
    （"华佗的著作有"里的"著作有"、"他后来在长"的"后来在"），
    而实体表是白名单 —— 候选不在表里就丢掉。
    副作用同样重要：用户随口提一个本书没有的人名时不会被继承进来，
    避免 query 漂移到不存在的实体上。
    """
    if not candidates:
        return []
    from app.models import Entity

    try:
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(Entity.name, Entity.aliases).where(Entity.novel_id == novel_id)
                )
            ).all()
    except Exception:  # noqa: BLE001
        logger.warning("实体校验失败，降级为不继承 novel=%s", novel_id, exc_info=True)
        return []

    if not rows:
        return []
    names: set[str] = set()
    for name, aliases in rows:
        if name:
            names.add(name)
        for a in aliases or []:
            if isinstance(a, str) and a:
                names.add(a)

    # 长候选优先：先命中长的再命中短的，避免"华佗"和"华佗的著"都进表
    out: list[str] = []
    for c in sorted(candidates, key=len, reverse=True):
        if c in names and c not in out:
            out.append(c)
    out.reverse()
    return out


async def _next_seq(db, session_id: str) -> int:
    return (
        await db.execute(
            select(func.coalesce(func.max(ChatMessage.seq), 0)).where(
                ChatMessage.session_id == session_id
            )
        )
    ).scalar() or 0


async def _touch_session(db, session_id: str, question: str) -> None:
    sess = await db.get(ChatSession, session_id)
    if sess is None:
        return
    sess.updated_at = _now_utc()
    # 首问定标题：取问题前 30 字，去掉换行
    if sess.title in ("", "新对话") and question.strip():
        sess.title = question.strip().replace("\n", " ")[:30]


async def save_question(session_id: str, question: str) -> int:
    """先落用户提问，返回它的 seq（回答用 seq+1 接在后面）。

    分开存的原因：生成阶段可能被中断（用户刷新页面、关标签页、点停止），
    而 SSE 响应被客户端断开时服务端生成器会被**直接取消**，走不到函数末尾的
    落库代码。先存提问能保证"问过什么"这一半记忆不丢，回答则尽力而为。
    """
    async with SessionLocal() as db:
        seq = await _next_seq(db, session_id) + 1
        db.add(
            ChatMessage(
                session_id=session_id,
                role="user",
                content=question,
                citations=[],
                seq=seq,
            )
        )
        await _touch_session(db, session_id, question)
        await db.commit()
    return seq


async def create_answer(session_id: str, seq: int) -> str:
    """先建一条**空**的助手消息，返回它的真实主键。

    为什么要提前建、而不是等答完再插：前端在流开始前只有一个本地临时 id
    （`a-<ts>-<rand>`），拿它调「点赞 / 删除」必 404；以前的解法是在 `done`
    事件里把 id 换掉，但 Semi 用 `key={item.id}` 渲染消息，换 id 等于卸载重建，
    答完那一瞬间正文会闪一下（先空一帧再出现）。

    改成开流前就把 id 交给前端（随 `meta` 下发）：切换发生在正文还是 0 个字的
    时候，视觉上什么都不会丢，`done` 也不需要再换 id。

    代价是生成彻底失败时库里会留一条空消息，由 `fill_answer()` 负责删掉。
    """
    async with SessionLocal() as db:
        msg = ChatMessage(
            session_id=session_id,
            role="assistant",
            content="",
            citations=[],
            seq=seq + 1,
        )
        db.add(msg)
        # 先 flush 拿 id 再 commit：commit 之后对象会 expire，
        # 在 async 会话里再访问属性会触发一次隐式 refresh，白跑一个 I/O
        await db.flush()
        mid = msg.id
        await db.commit()
    return mid


async def fill_answer(message_id: str, answer: str, citations: list[dict]) -> None:
    """把生成结果写回 `create_answer()` 预建的那条消息。

    `answer` 为空表示这一轮什么也没产出（异常 / 客户端立刻断开）：直接删掉
    预建的空壳。留着会让回放里出现一个空气泡，摘要与轮次配对也会多算一轮。
    """
    async with SessionLocal() as db:
        msg = await db.get(ChatMessage, message_id)
        if msg is None:
            return
        if not (answer or "").strip():
            await db.delete(msg)
            await db.commit()
            return
        msg.content = answer
        msg.citations = citations or []
        await db.commit()


async def save_answer(
    session_id: str,
    seq: int,
    answer: str,
    citations: list[dict],
) -> str:
    """一次存完整回答（预建 + 填充），返回消息 id；空回答返回空串。

    SSE 主链路不走这里 —— 它需要在开流前就拿到 id，所以拆成
    `create_answer()` + `fill_answer()` 两步。这个组合版留给
    `save_turn()`（测试与离线补录）用。
    """
    if not answer.strip():
        return ""
    mid = await create_answer(session_id, seq)
    await fill_answer(mid, answer, citations)
    return mid


async def drop_last_turn(session_id: str, expect: str = "") -> str:
    """删掉会话最后一轮问答（最后一条 user 及其后的消息），返回被删的提问内容。

    「重新生成」用它：不删的话历史里会多一条重复提问 + 两条答案，
    下一轮提问时模型会看到"同一个问题被问了两次、答了两遍"，
    上下文直接变脏。

    `expect` 非空时校验被删的提问是否就是它。前端只在最后一条答案上给"重新生成"
    入口，正常不会错位；但两个标签页同时操作、或点了重新生成又切了会话，
    就可能删掉别的问题——这种时候宁可不删（返回空串让调用方退回普通提问），
    也不要静默删掉用户另一段对话。

    返回：被删掉的提问内容；没有可删的回合时返回空串。
    """
    if not session_id:
        return ""
    async with SessionLocal() as db:
        last_user = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id, ChatMessage.role == "user")
                .order_by(desc(ChatMessage.seq), desc(ChatMessage.id))
                .limit(1)
            )
        ).scalar_one_or_none()
        if last_user is None:
            return ""
        if expect and last_user.content.strip() != expect.strip():
            logger.info(
                "重新生成跳过：最后一轮提问是 %r，不是 %r 会话=%s",
                last_user.content[:30],
                expect[:30],
                session_id,
            )
            return ""
        seq = int(last_user.seq or 0)
        await db.execute(
            delete(ChatMessage).where(
                ChatMessage.session_id == session_id, ChatMessage.seq >= seq
            )
        )
        await db.commit()
    return last_user.content


async def save_turn(
    session_id: str,
    question: str,
    answer: str,
    citations: list[dict],
) -> None:
    """一次存完整回合（提问 + 回答）。测试与离线补录用，SSE 主链路走拆分版。"""
    seq = await save_question(session_id, question)
    await save_answer(session_id, seq, answer, citations)


async def add_usage(session_id: str, prompt: int, completion: int, calls: int) -> None:
    """把一轮（或多次）LLM 调用的 token 累加进会话。

    用 SQL 的 `SET x = x + n` 而不是"读出来加完再写回"：
    问答回复和后台摘要可能同时结束，读-改-写会丢掉其中一边。
    """
    if not session_id or (not prompt and not completion and not calls):
        return
    async with SessionLocal() as db:
        await db.execute(
            update(ChatSession)
            .where(ChatSession.id == session_id)
            .values(
                prompt_tokens=ChatSession.prompt_tokens + int(prompt or 0),
                completion_tokens=ChatSession.completion_tokens + int(completion or 0),
                llm_calls=ChatSession.llm_calls + int(calls or 0),
            )
        )
        await db.commit()


def _now_utc():
    from datetime import UTC, datetime

    return datetime.now(UTC)


async def session_usage(session_id: str) -> dict:
    """读会话的累计用量与摘要状态（一次查询，给前端回显）。

    单独抽出来是因为 `done` 事件要同时带"本轮"和"累计"两个数：
    本轮来自 `flush_usage()`，累计必须从库上读（`add_usage` 是 SQL 侧自增，
    Python 里没有新的累计值）。
    """
    if not session_id:
        return {}
    async with SessionLocal() as db:
        row = (
            await db.execute(
                select(
                    ChatSession.prompt_tokens,
                    ChatSession.completion_tokens,
                    ChatSession.llm_calls,
                    ChatSession.summary_seq,
                    func.coalesce(func.length(ChatSession.summary), 0),
                ).where(ChatSession.id == session_id)
            )
        ).first()
    if row is None:
        return {}
    prompt, completion, calls, summary_seq, summary_chars = row
    prompt = int(prompt or 0)
    completion = int(completion or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "calls": int(calls or 0),
        "summary_seq": int(summary_seq or 0),
        "summary_chars": int(summary_chars or 0),
    }


async def load_summary(session_id: str) -> tuple[str, int]:
    """取会话的滚动摘要，以及它已经覆盖到哪条消息（seq）。"""
    if not session_id:
        return "", 0
    async with SessionLocal() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            return "", 0
        return (sess.summary or ""), int(sess.summary_seq or 0)


SUMMARY_SYSTEM = """你在压缩一段多轮对话，供后续提问继续使用。

要求：
1. 只保留后续可能用到的信息：用户问过什么、结论是什么、涉及的人物/章节等专有名词。
2. 保留用户的原话意图（他用什么词问的），但不要逐字复述整段问答。
3. 已经确认"资料未提及"的事项要明确写出来，避免后面重复检索、重复否定。
4. 400 字以内，要点式中文。不要编造原文没有的内容，不要写"用户很满意"这类废话。
"""


async def _summarize(prev: str, turns: list[dict], usage_tag: str | None = None) -> str:
    from app.services.llm.client import get_llm

    lines: list[str] = []
    for t in turns:
        if t["role"] == "user":
            lines.append(f"用户：{t['question']}")
        else:
            content = re.sub(r"\[资料\d+\]", "", t["content"])
            if len(content) > 500:
                content = content[:500] + "…"
            lines.append(f"助手：{content}")
    messages = [
        {"role": "system", "content": SUMMARY_SYSTEM},
        {
            "role": "user",
            "content": f"【已有摘要】\n{prev or '（无）'}\n\n【需要并进摘要的对话】\n"
            + "\n".join(lines),
        },
    ]
    text = await get_llm().complete(
        messages,
        temperature=0.1,
        max_tokens=800,
        model=settings.chat_summary_model,
        usage_tag=usage_tag,  # 摘要也是这个会话的开销，一并记账
    )
    return text.strip()


async def flush_usage(session_id: str) -> dict:
    """把 `LLMClient` 里属于这个会话的用量落到会话行上，并清空累计器。

    问答正文和后台摘要都会调这里：谁先跑完谁取走，`add_usage` 用的是
    `SET x = x + n`，所以先取后取都不丢。清空是必须的 —— 否则长跑进程里
    这个字典会按会话无限增长。

    返回**本次**取走并落账的用量（空 dict = 这一轮没有新消耗）。
    返回值是给 `done` 事件用的：前端要在这轮答完的那一刻就显示消耗，
    不能靠它再查一次会话列表（落账与刷新之间有时序差，而且多一次请求）。
    """
    from app.services.llm.client import get_llm

    usage = get_llm().pop_usage(session_id)
    if not usage:
        return {}
    await add_usage(
        session_id,
        int(usage.get("prompt_tokens") or 0),
        int(usage.get("completion_tokens") or 0),
        int(usage.get("calls") or 0),
    )
    return {
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "total_tokens": int(usage.get("total_tokens") or 0),
        "calls": int(usage.get("calls") or 0),
    }


async def maybe_summarize(session_id: str) -> bool:
    """把挤出 prompt 窗口的早期回合压成摘要。返回是否真的压了。

    触发条件看的是**未被摘要覆盖的回合数**，不是总回合数：
    压过一次之后 `summary_seq` 前进，剩下的新回合重新计数，
    否则每轮都会触发一次摘要（又慢又费钱）。
    """
    if not session_id or not settings.chat_summary_enabled:
        return False
    async with SessionLocal() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            return False
        prev = sess.summary or ""
        covered = int(sess.summary_seq or 0)
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id, ChatMessage.seq > covered)
                .order_by(ChatMessage.seq)
            )
        ).scalars().all()

    turns = _pair_turns(list(rows))
    # turns 是**消息**列表（一问一答各一条），阈值是回合数，必须 ×2。
    # 这里若按 len(turns) 比，trigger=8 实际只等到 4 个回合就压缩，
    # 而且 turns[:-keep] 也只砍掉 keep 条消息而不是 keep 个回合。
    if len(turns) <= settings.chat_summary_trigger_turns * 2:
        return False

    keep = max(0, settings.chat_summary_keep_turns)
    batch = turns[: -keep * 2] if keep else turns
    if not batch:
        return False

    try:
        new_summary = await _summarize(prev, batch, usage_tag=session_id)
    except Exception:  # noqa: BLE001
        # 摘要失败不影响问答主链路：下一轮还会再试
        logger.warning("会话摘要失败 session=%s", session_id, exc_info=True)
        return False
    finally:
        await flush_usage(session_id)
    if not new_summary:
        return False

    upto = int(batch[-1].get("seq") or 0)
    async with SessionLocal() as db:
        sess = await db.get(ChatSession, session_id)
        if sess is None:
            return False
        sess.summary = new_summary
        sess.summary_seq = upto
        await db.commit()
    logger.info(
        "会话摘要已更新 session=%s 覆盖到 seq=%s 压缩回合=%d 字数=%d",
        session_id,
        upto,
        len(batch) // 2,
        len(new_summary),
    )
    return True


def _pair_turns(rows: list) -> list[dict]:
    """把消息序列配成完整回合（user + assistant），丢弃半截回合。

    半截（只有 user 没 assistant）说明那轮被中断了 —— 塞进历史或摘要里
    会让模型以为"上一轮问过但没回答"，进而跑去重答一遍。
    """
    out: list[dict] = []
    i = 0
    while i < len(rows) - 1:
        cur, nxt = rows[i], rows[i + 1]
        if cur.role == "user" and nxt.role == "assistant":
            out.append(
                {
                    "role": "user",
                    "content": cur.content,
                    "question": cur.content,
                    "citations": [],
                    "status": "ok",
                    "seq": int(cur.seq or 0),
                }
            )
            out.append(
                {
                    "role": "assistant",
                    "content": nxt.content,
                    "question": "",
                    "citations": nxt.citations or [],
                    "status": "ok",
                    "seq": int(nxt.seq or 0),
                }
            )
            i += 2
        else:
            i += 1
    return out


async def load_turns(session_id: str, limit: int = HISTORY_TURNS) -> list[dict]:
    """回放最近若干**完整回合**。

    按 `seq` 排序而不是 `created_at`：同一回合的 user/assistant 可能落在
    同一微秒（实测有 2 组），按时间排会偶发把 assistant 排到 user 前面，
    回放出来的对话是倒的，模型读到会当成"用户先回答"。

    截断时只取完整回合 —— 半截回合（只有 user 没有 assistant，或反之）
    塞进历史会让模型以为上一轮被中断过。
    """
    if not session_id:
        return []
    async with SessionLocal() as db:
        rows = (
            await db.execute(
                select(ChatMessage)
                .where(ChatMessage.session_id == session_id)
                .order_by(ChatMessage.seq.desc(), ChatMessage.id.desc())
                .limit(limit * 2 + 4)
            )
        ).scalars().all()

    rows = list(reversed(rows))
    # 从尾部往前凑完整回合（user+assistant 配对）。
    # 计数用**消息条数**：每轮插入 2 条（user + assistant），
    # 若按 len(turns) < limit 计，limit=6 实际只会取到 3 个回合。
    turns: list[dict] = []
    i = len(rows) - 1
    while i >= 0 and len(turns) < limit * 2:
        m = rows[i]
        if m.role != "assistant":
            i -= 1
            continue
        # 往前找配对的 user
        j = i - 1
        while j >= 0 and rows[j].role == "assistant":
            j -= 1
        if j < 0:
            break
        user = rows[j]
        turns.insert(
            0,
            {
                "role": "user",
                "content": user.content,
                "question": user.content,
                "citations": [],
                "status": "ok",
                "seq": int(user.seq or 0),
            },
        )
        turns.insert(
            1,
            {
                "role": "assistant",
                "content": m.content,
                "question": "",
                "citations": m.citations or [],
                "status": "ok",
                "seq": int(m.seq or 0),
            },
        )
        i = j - 1
    return turns


async def context_entities(
    novel_id: str,
    session_id: str,
    limit: int = MAX_CONTEXT_ENTITIES,
) -> list[str]:
    """从历史里提取"当前话题的实体"，供检索 query 继承。

    取**最近一轮**问句里的实体，而不是全部历史：话题会漂移，
    十轮前聊的实体多半跟当前问题无关。用最近一轮 + 近期窗口兜底，
    取出现频次最高的几个。

    候选必须经实体表校验（见 `_verified_entities`），所以返回的
    都是这本书里真实存在的实体名。
    """
    if not session_id:
        return []
    turns = await load_turns(session_id, limit=2)
    if not turns:
        return []
    questions = [t["question"] for t in turns if t["role"] == "user" and t.get("question")]
    if not questions:
        return []
    joined = " ".join(questions)
    candidates = _extract_entities(joined)
    if not candidates:
        return []
    verified = await _verified_entities(novel_id, candidates)
    return verified[:limit]


def build_memory_block(turns: list[dict], summary: str = "") -> str:
    """把历史回合渲染成 prompt 里的一段文本。

    刻意不用 OpenAI 的多轮 messages 格式，而是压成一段"对话摘要"：
    多轮格式下每轮都是独立的 role/content，模型容易把历史里的
    "【资料】"当成当前轮的证据；压成一段并明确标注是历史，
    能显著降低"用旧资料的编号回答新问题"的概率。

    `summary` 是被挤出窗口的更早回合压成的摘要，放在最前面并标明
    "更早"，让模型知道那部分信息不完整、但确实发生过。
    """
    if not turns and not summary:
        return ""
    lines = ["【历史对话】"]
    if summary:
        lines.append(f"（更早的对话摘要，非原文）\n{summary}")
    for t in turns:
        if t["role"] == "user":
            lines.append(f"用户：{t['question']}")
        else:
            content = t["content"]
            # 历史答案里的 [资料N] 编号在当前轮没有对应内容，
            # 留着会让模型去引用不存在的资料。剥掉。
            content = re.sub(r"\[资料\d+\]", "", content)
            if len(content) > 600:
                content = content[:600] + "…"
            lines.append(f"助手：{content}")
    return "\n".join(lines)
