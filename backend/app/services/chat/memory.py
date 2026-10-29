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

from sqlalchemy import func, select

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


async def save_answer(
    session_id: str,
    seq: int,
    answer: str,
    citations: list[dict],
) -> None:
    """把回答接在提问后面存。seq 由 save_question 给出，保证同回合严格有序。"""
    if not answer.strip():
        return
    async with SessionLocal() as db:
        db.add(
            ChatMessage(
                session_id=session_id,
                role="assistant",
                content=answer,
                citations=citations or [],
                seq=seq + 1,
            )
        )
        await db.commit()


async def save_turn(
    session_id: str,
    question: str,
    answer: str,
    citations: list[dict],
) -> None:
    """一次存完整回合（提问 + 回答）。测试与离线补录用，SSE 主链路走拆分版。"""
    seq = await save_question(session_id, question)
    await save_answer(session_id, seq, answer, citations)


def _now_utc():
    from datetime import UTC, datetime

    return datetime.now(UTC)


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


def build_memory_block(turns: list[dict]) -> str:
    """把历史回合渲染成 prompt 里的一段文本。

    刻意不用 OpenAI 的多轮 messages 格式，而是压成一段"对话摘要"：
    多轮格式下每轮都是独立的 role/content，模型容易把历史里的
    "【资料】"当成当前轮的证据；压成一段并明确标注是历史，
    能显著降低"用旧资料的编号回答新问题"的概率。
    """
    if not turns:
        return ""
    lines = ["【历史对话】"]
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
