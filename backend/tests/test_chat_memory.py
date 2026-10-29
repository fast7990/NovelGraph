"""会话记忆的回归测试。

覆盖三个曾经出错的点：
1. 回合排序：同一回合的 user/assistant 可能落在同一微秒，
   按 created_at 排会偶发倒序 → 必须按 seq 排。
2. 上下文实体继承：指代型追问要能把历史实体带进检索 query。
3. 停用词过滤：泛词不能当实体继承（否则 query 变成噪声）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.chat import memory  # noqa: E402
from app.services.chat.rag import _expand_query  # noqa: E402


class _Msg:
    """ChatMessage 的最小替身，供 load_turns 的排序/配对逻辑测试用。"""

    def __init__(self, seq: int, role: str, content: str, citations=None):
        self.id = f"cm_{seq:04d}"
        self.seq = seq
        self.role = role
        self.content = content
        self.citations = citations or []
        self.created_at = None


class TestEntityExtraction:
    def test_extracts_name(self):
        # 候选里必须包含"华佗"（短名优先）
        assert "华佗" in memory._extract_entities("华佗的著作有哪些")

    def test_filters_stopwords(self):
        # "小说" "章节" 这类泛词不能被当实体
        out = memory._extract_entities("这本小说的章节主角是谁")
        assert "小说" not in out
        assert "章节" not in out
        assert "主角" not in out

    def test_max_length(self):
        # 超过 6 字的连续汉段多半是句子，不是专名
        out = memory._extract_entities("他后来在长坂坡之战中投奔了刘备阵营")
        assert all(len(x) <= 6 for x in out)

    def test_skips_anaphora(self):
        out = memory._extract_entities("他们三个人的关系")
        assert "他们" not in out

    def test_drops_trailing_particle_candidates(self):
        """以虚词结尾的候选不是专名 —— 这是主要噪声来源。"""
        out = memory._extract_entities("华佗的著作有哪些")
        for bad in ("著作有", "作有", "有哪"):
            assert bad not in out


class TestQueryExpansion:
    def test_anaphora_gets_entity_prefixed(self):
        """指代型追问要把历史实体拼进 query。"""
        qs = _expand_query("他后来怎么样了", ["华佗"])
        assert len(qs) == 1
        assert "华佗" in qs[0]
        assert "他" not in qs[0]

    def test_pure_anaphora_falls_back_to_entities(self):
        """整句都是指代词时，直接用实体做 query。"""
        qs = _expand_query("那呢", ["关羽"])
        assert qs == ["关羽"]

    def test_no_entities_returns_empty(self):
        assert _expand_query("他后来怎么样了", []) == []

    def test_non_anaphora_not_inherited(self):
        """独立问题不该被污染 —— 不相关实体会稀释检索主题。"""
        assert _expand_query("曹操和刘备是什么关系", ["华佗"]) == []


class TestMemoryBlock:
    def test_strips_stale_citation_markers(self):
        """历史答案里的 [资料N] 在当前轮没有对应内容，必须剥掉。"""
        block = memory.build_memory_block(
            [
                {"role": "user", "content": "问题", "question": "问题", "citations": []},
                {
                    "role": "assistant",
                    "content": "根据资料回答 [资料1][资料3]",
                    "question": "",
                    "citations": [],
                },
            ]
        )
        assert "[资料1]" not in block
        assert "[资料3]" not in block

    def test_empty_turns(self):
        assert memory.build_memory_block([]) == ""

    def test_truncates_long_answer(self):
        block = memory.build_memory_block(
            [
                {"role": "user", "content": "q", "question": "q", "citations": []},
                {"role": "assistant", "content": "长" * 2000, "question": "", "citations": []},
            ]
        )
        assert len(block) < 1000


class TestTurnCounting:
    """回合数与消息条数不能混淆 —— 一个回合是 2 条消息。

    曾经的 bug：`while len(turns) < limit` 里 turns 每轮插 2 条，
    limit=6 实际只取到 3 个回合，历史比预期少一半。
    """

    def test_limit_counts_turns_not_messages(self, monkeypatch):
        """limit=3 应当取回 3 个完整回合（6 条消息）。"""
        rows = []
        for i in range(10):
            rows.append(_Msg(seq=i * 2 + 1, role="user", content=f"问{i}", citations=[]))
            rows.append(_Msg(seq=i * 2 + 2, role="assistant", content=f"答{i}", citations=[]))

        class _ScalarResult:
            def __init__(self, items):
                self._items = items

            def scalars(self):
                return self

            def all(self):
                return self._items

        class _Ctx:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def execute(self, *a, **k):
                # 模拟真实 SQL：按 seq 倒序 + LIMIT
                ordered = sorted(rows, key=lambda r: r.seq, reverse=True)
                cap = k.get("limit") or a[1] if len(a) > 1 else None
                return _ScalarResult(ordered[:cap] if cap else ordered)

        monkeypatch.setattr(memory, "SessionLocal", lambda: _Ctx())
        import asyncio

        out = asyncio.run(memory.load_turns("cs_x", limit=3))
        # 3 个回合 = 6 条消息，且取的是**最近**的 3 回合（第 7~9 回合）
        assert len(out) == 6
        assert out[0]["content"] == "问7"
        assert out[-1]["content"] == "答9"




class _FakeSession:
    """ChatSession 的最小替身：只带摘要相关字段。"""

    def __init__(self, summary: str = "", summary_seq: int = 0):
        self.id = "cs_fake"
        self.summary = summary
        self.summary_seq = summary_seq


class _FakeDB:
    """够用的假会话上下文：get() 返回同一个 session，execute() 返回给定消息。"""

    def __init__(self, sess, rows):
        self.sess = sess
        self.rows = rows
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, _model, _pk):
        return self.sess

    async def execute(self, *_a, **_k):
        rows = self.rows

        class _R:
            def scalars(self_inner):
                return self_inner

            def all(self_inner):
                return rows

        return _R()

    async def commit(self):
        self.committed += 1


def _turns_rows(n: int, start_seq: int = 1):
    rows = []
    seq = start_seq
    for i in range(n):
        rows.append(_Msg(seq=seq, role="user", content=f"问{i}"))
        seq += 1
        rows.append(_Msg(seq=seq, role="assistant", content=f"答{i}"))
        seq += 1
    return rows


class TestPairTurns:
    def test_drops_dangling_turn(self):
        """半截回合（只有用户提问、没有回答）不能进历史/摘要。"""
        rows = [
            _Msg(seq=1, role="user", content="问一"),
            _Msg(seq=2, role="assistant", content="答一"),
            _Msg(seq=3, role="user", content="问二被中断"),
        ]
        turns = memory._pair_turns(rows)
        assert len(turns) == 2
        assert turns[0]["question"] == "问一"

    def test_carries_seq(self):
        turns = memory._pair_turns(_turns_rows(1))
        assert [t["seq"] for t in turns] == [1, 2]


class TestRollingSummary:
    """长对话不能只截 6 轮 —— 被挤出去的早期回合要压成摘要，否则凭空消失。"""

    def test_skips_below_trigger(self, monkeypatch):
        sess = _FakeSession()
        db = _FakeDB(sess, _turns_rows(4))  # 4 回合 < 触发阈值 8
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)
        import asyncio

        assert asyncio.run(memory.maybe_summarize("cs_fake")) is False
        assert sess.summary == ""
        assert db.committed == 0

    def test_summarizes_old_turns_and_advances_seq(self, monkeypatch):
        sess = _FakeSession()
        rows = _turns_rows(12)  # 12 回合 = 24 条消息，seq 1..24
        db = _FakeDB(sess, rows)
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)
        captured = {}

        async def fake_summarize(prev, turns, usage_tag=None):
            captured["prev"] = prev
            captured["turns"] = turns
            return "摘要是这样"

        monkeypatch.setattr(memory, "_summarize", fake_summarize)
        import asyncio

        assert asyncio.run(memory.maybe_summarize("cs_fake")) is True
        assert sess.summary == "摘要是这样"
        # 保留最近 keep=3 回合原文，其余 9 回合进摘要 → 覆盖到第 18 条消息
        assert sess.summary_seq == 18
        assert len(captured["turns"]) == 18

    def test_keeps_recent_turns_out_of_summary(self, monkeypatch):
        sess = _FakeSession()
        db = _FakeDB(sess, _turns_rows(12))
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)
        captured = {}

        async def fake_summarize(prev, turns, usage_tag=None):
            captured["turns"] = turns
            return "x"

        monkeypatch.setattr(memory, "_summarize", fake_summarize)
        import asyncio

        asyncio.run(memory.maybe_summarize("cs_fake"))
        questions = [t["question"] for t in captured["turns"] if t["role"] == "user"]
        # 最近 3 轮（问9/问10/问11）必须留在窗口里，不能被压掉
        assert "问11" not in questions
        assert "问10" not in questions
        assert "问9" not in questions
        assert "问0" in questions

    def test_feeds_previous_summary_back(self, monkeypatch):
        """二次压缩要把旧摘要带上，否则早前的内容会被覆盖掉。"""
        sess = _FakeSession(summary="旧摘要", summary_seq=4)
        db = _FakeDB(sess, _turns_rows(10, start_seq=5))
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)
        captured = {}

        async def fake_summarize(prev, turns, usage_tag=None):
            captured["prev"] = prev
            return "新摘要"

        monkeypatch.setattr(memory, "_summarize", fake_summarize)
        import asyncio

        asyncio.run(memory.maybe_summarize("cs_fake"))
        assert captured["prev"] == "旧摘要"

    def test_summary_failure_keeps_session_untouched(self, monkeypatch):
        sess = _FakeSession()
        db = _FakeDB(sess, _turns_rows(12))
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)

        async def boom(prev, turns, usage_tag=None):
            raise RuntimeError("上游炸了")

        monkeypatch.setattr(memory, "_summarize", boom)
        # flush_usage 会碰真数据库，这里一并替换掉
        async def noop_flush(_sid):
            return None

        monkeypatch.setattr(memory, "flush_usage", noop_flush)
        import asyncio

        assert asyncio.run(memory.maybe_summarize("cs_fake")) is False
        assert sess.summary == ""
        assert db.committed == 0


class TestMemoryBlockWithSummary:
    def test_includes_summary_before_turns(self):
        block = memory.build_memory_block(
            [{"role": "user", "content": "问", "question": "问", "citations": []}],
            summary="早先聊过华佗的结局",
        )
        assert "更早的对话摘要" in block
        assert "早先聊过华佗的结局" in block

    def test_summary_only_still_renders(self):
        """窗口里一条原文回合都没有时，摘要本身仍要有输出。"""
        block = memory.build_memory_block([], summary="只有摘要")
        assert "只有摘要" in block


# ---------- 重新生成 ----------


class _SelectResult:
    def __init__(self, v):
        self.v = v

    def scalar_one_or_none(self):
        return self.v


class _DropDB:
    """专门给 drop_last_turn 用的假上下文：能分清 select 与 delete。"""

    def __init__(self, last_user):
        self.last_user = last_user
        self.deleted = 0
        self.committed = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def execute(self, stmt, *a, **k):
        if str(stmt).lstrip().upper().startswith("DELETE"):
            self.deleted += 1
            return _SelectResult(None)
        return _SelectResult(self.last_user)

    async def commit(self):
        self.committed += 1


class TestDropLastTurn:
    """「重新生成」要先把上一轮删掉。

    不删的话历史里会多出一条重复提问和两条答案，下一轮提问时模型会看到
    "同一个问题被问了两次、答了两遍"，上下文直接变脏。
    """

    def test_drops_last_turn(self, monkeypatch):
        db = _DropDB(_Msg(seq=5, role="user", content="华佗的著作有哪些"))
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)
        out = _run(memory.drop_last_turn("cs_fake", expect="华佗的著作有哪些"))
        assert out == "华佗的著作有哪些"
        assert db.deleted == 1
        assert db.committed == 1

    def test_refuses_when_question_differs(self, monkeypatch):
        """最后一轮不是这个问题就不删 —— 宁可退回普通提问，也不能静默删掉
        用户另一段对话（两个标签页同时操作、或点了重新生成又切了会话）。"""
        db = _DropDB(_Msg(seq=5, role="user", content="完全无关的问题"))
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)
        out = _run(memory.drop_last_turn("cs_fake", expect="华佗的著作有哪些"))
        assert out == ""
        assert db.deleted == 0

    def test_empty_session_is_noop(self, monkeypatch):
        db = _DropDB(None)
        monkeypatch.setattr(memory, "SessionLocal", lambda: db)
        assert _run(memory.drop_last_turn("cs_fake", expect="随便")) == ""
        assert db.deleted == 0


def _run(coro):
    import asyncio

    return asyncio.run(coro)
