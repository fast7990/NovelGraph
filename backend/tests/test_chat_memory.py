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


