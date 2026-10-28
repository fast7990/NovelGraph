"""ORM 模型。P1 起所有数据都在 PostgreSQL：

- `novels / chapters / chunks / chunk_reports / jobs / chat_*`：元数据与切片正文
  （唯一事实来源，检索结果最终都回到 `chunks` 取原文）
- `chunk_vectors`：可检索单元（child / 章摘要）的向量，pgvector 存储
- `chunk_lexical`：同一批单元的词法倒排（bigram → tsvector）
- `entities / relations / entity_mentions`：知识图谱（P1-M5）

**图谱为什么不用 Neo4j**：本机没有 JVM，装 Neo4j 要先拉 JDK + 服务端约 330MB，
且会引出第三个存储进程、删书时跨库一致性得靠应用层补偿。而这里需要的图查询只有
"实体定位 + N 跳邻域 + 时间线"三种，规模是单书千级实体、数千条边——PG 的递归 CTE
在这个量级是毫秒级，且外键 CASCADE 让删书一句话搞定。真要做社区发现/PageRank 那类
图算法时再换，`services/graph/store.py` 是可替换的单点。

`chunk_vectors` / `chunk_lexical` / `entities` / `relations` 都对上游加了外键：
删书时 PG 会自己收拾干净，不再需要应用层"记得删向量/删图"。
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from app.config import settings
from app.db import Base


def _now() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


class _TZ:
    """所有时间列统一 timestamptz。

    SQLite 时代没有时区概念，迁到 PG 后如果还写成 TIMESTAMP WITHOUT TIME ZONE，
    asyncpg 会拒绝写入带 tzinfo 的 datetime（而 `_now()` 返回的就是 aware），
    第一次跑入库就会炸在 commit 上。统一带时区，省掉一整类问题。
    """

    @staticmethod
    def col(**kwargs):  # noqa: ANN003
        return mapped_column(DateTime(timezone=True), **kwargs)


class Novel(Base):
    __tablename__ = "novels"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("nv"))
    title: Mapped[str] = mapped_column(String(200), default="")
    filename: Mapped[str] = mapped_column(String(255), default="")
    storage_key: Mapped[str] = mapped_column(String(512), default="")
    file_size: Mapped[int] = mapped_column(Integer, default=0)
    encoding: Mapped[str] = mapped_column(String(32), default="")
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    chapter_count: Mapped[int] = mapped_column(Integer, default=0)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _TZ.col(default=_now)
    updated_at: Mapped[datetime] = _TZ.col(default=_now, onupdate=_now)


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("job"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    # 任务种类：ingest（解析→切片→向量化）/ rebuild（只重算向量）/ graph（图谱抽取）
    kind: Mapped[str] = mapped_column(String(20), default="ingest")
    status: Mapped[str] = mapped_column(String(20), default="PENDING")  # PENDING/RUNNING/COMPLETED/FAILED/CANCELED
    stage: Mapped[str] = mapped_column(String(20), default="PENDING")
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    # 切片完成度以字符为准：块数在流式切分中事前未知，用块数会出现"越跑越退"
    chars_done: Mapped[int] = mapped_column(Integer, default=0)
    chars_total: Mapped[int] = mapped_column(Integer, default=0)
    chunks_created: Mapped[int] = mapped_column(Integer, default=0)
    chapters_done: Mapped[int] = mapped_column(Integer, default=0)
    chapters_total: Mapped[int] = mapped_column(Integer, default=0)
    vectors_written: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = _TZ.col(default=_now)
    started_at: Mapped[datetime | None] = _TZ.col(default=None)
    finished_at: Mapped[datetime | None] = _TZ.col(default=None)


class Chapter(Base):
    __tablename__ = "chapters"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ch"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, default=1)
    title: Mapped[str] = mapped_column(String(200), default="")
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    char_count: Mapped[int] = mapped_column(Integer, default=0)
    summary: Mapped[str] = mapped_column(Text, default="")

    __table_args__ = (Index("ix_chapters_novel_no", "novel_id", "chapter_no"),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ck"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    parent_id: Mapped[str | None] = mapped_column(String(40), default=None, index=True)
    level: Mapped[int] = mapped_column(Integer, default=1)  # 0=parent 1=child 3=章摘要
    chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    seq: Mapped[int] = mapped_column(Integer, default=0)
    title: Mapped[str] = mapped_column(String(200), default="")
    content: Mapped[str] = mapped_column(Text, default="")
    char_start: Mapped[int] = mapped_column(Integer, default=0)
    char_end: Mapped[int] = mapped_column(Integer, default=0)
    token_est: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (
        Index("ix_chunks_novel_level", "novel_id", "level"),
        Index("ix_chunks_novel_chapter", "novel_id", "chapter_no", "seq"),
    )


class ChunkVector(Base):
    """向量单独成表，而不是塞进 `chunks`。

    理由：入库是"先写正文、再逐批补向量"的两段式，如果把 4KB 的向量放进 chunks 行里，
    每批 update 都会重写整行并触发 TOAST 检查，2 万块的书会攒下大量死元组；
    分开后 chunks 保持只读，向量表只管自己的插入与 HNSW 维护。
    """

    __tablename__ = "chunk_vectors"

    chunk_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("chunks.id", ondelete="CASCADE", name="fk_chunk_vectors_chunk"),
        primary_key=True,
    )
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    # 维度与 embedding 模型绑定：换模型（512 ↔ 1024）必须重建这张表，
    # 建表时把维度写进列类型，插错维度 PG 会直接拒绝，比应用层校验更可靠。
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dim), nullable=False)


class ChunkLexical(Base):
    """词法倒排：一个 child 块一行。

    `tsv` 存的是**bigram 切分后**的 tsvector（见 services/retrieval/lexical.py），
    不是原文直接 to_tsvector —— PG 默认分词器对中文整段当一个 token，
    不做切分的话"陆沉"这种两字词根本进不了倒排表。
    """

    __tablename__ = "chunk_lexical"

    chunk_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("chunks.id", ondelete="CASCADE", name="fk_chunk_lexical_chunk"),
        primary_key=True,
    )
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    # 保留切分后的原始串：排查"某个词为什么搜不到"时可以直接看一眼切成了什么，
    # 也方便以后换分词方案时不用回表重算
    bigrams: Mapped[str] = mapped_column(Text, default="")
    tsv: Mapped[str] = mapped_column(TSVECTOR, nullable=False)


class ChunkReport(Base):
    """切片质量告警。前端"完成度"页的第二块内容。"""

    __tablename__ = "chunk_reports"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rp"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    job_id: Mapped[str] = mapped_column(String(40), index=True)
    chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    code: Mapped[str] = mapped_column(String(40), default="")
    severity: Mapped[str] = mapped_column(String(10), default="warn")  # info/warn/error
    detail: Mapped[str] = mapped_column(Text, default="")
    char_start: Mapped[int] = mapped_column(Integer, default=0)


class ChatSession(Base):
    __tablename__ = "chat_sessions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cs"))
    novel_id: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(String(200), default="新对话")
    created_at: Mapped[datetime] = _TZ.col(default=_now)


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("cm"))
    session_id: Mapped[str] = mapped_column(String(40), index=True)
    role: Mapped[str] = mapped_column(String(20), default="user")
    content: Mapped[str] = mapped_column(Text, default="")
    citations: Mapped[list] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = _TZ.col(default=_now)


# ---------------------------------------------------------------- 知识图谱（P1-M5）

# 实体类型固定六类。用英文枚举而不是中文，是因为服务层要按类型分支
# （人物才配 importance、事件才有点的时间线），代码里比对字符串比比对中文稳。
ENTITY_TYPES = ("Character", "Faction", "Location", "Item", "Event", "Concept")

# 关系词反过来用中文：它本质是"这两个人是什么关系"的语义标签，
# 中文小说里"师徒/道侣/宿敌"这类词的信息量比 MASTER_DISCIPLE 更细，
# 而且模型直接输出中文的准确率明显更高，少一层枚举映射就少一处漂移。
# 前 17 个是人对人，后 4 个是跨类型的结构关系（人→势力/事件/地点/物品）。
RELATION_TYPES = (
    "师徒", "父子", "母子", "父女", "母女", "兄弟", "姐妹", "夫妻", "道侣",
    "爱慕", "敌对", "盟友", "上下级", "同门", "朋友", "主仆", "亲戚",
    "归属", "参与", "位于", "持有", "OTHER",
)


class Entity(Base):
    """规范实体。同名不同称谓在这一层合并（`aliases` 记原名）。

    唯一键是 `(novel_id, type, name)` 而不是 `(novel_id, name)`：
    "青云"既可能是门派也可能是地名，只按名字唯一会把两类强行合并。
    """

    __tablename__ = "entities"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("ent"))
    novel_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("novels.id", ondelete="CASCADE", name="fk_entities_novel"), index=True
    )
    name: Mapped[str] = mapped_column(String(120))
    type: Mapped[str] = mapped_column(String(20), default="Character")
    aliases: Mapped[list] = mapped_column(JSONB, default=list)
    desc: Mapped[str] = mapped_column(Text, default="")
    first_chapter: Mapped[int] = mapped_column(Integer, default=0)
    # 归一化到 0-1 的重要度（按提及频次对数缩放），前端节点大小直接用
    importance: Mapped[float] = mapped_column(Float, default=0.0)
    mention_count: Mapped[int] = mapped_column(Integer, default=0)
    meta: Mapped[dict] = mapped_column(JSONB, default=dict)

    __table_args__ = (
        UniqueConstraint("novel_id", "type", "name", name="uq_entities_novel_type_name"),
        Index("ix_entities_novel_type", "novel_id", "type"),
        Index("ix_entities_novel_importance", "novel_id", "importance"),
    )


class Relation(Base):
    """有向边。双向关系（盟友/夫妻）由抽取层写成一条，展示时前端当无向处理。

    `chunk_id` 用 SET NULL 而不是 CASCADE：重新入库会重建 chunks，
    若级联删边，一次切片参数微调就把几千条 LLM 抽出来的边全清空了（很贵）。
    宁可留"有边无证据"，也不让正文重跑牵动图谱。
    """

    __tablename__ = "relations"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("rel"))
    novel_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("novels.id", ondelete="CASCADE", name="fk_relations_novel"), index=True
    )
    src_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("entities.id", ondelete="CASCADE", name="fk_relations_src"), index=True
    )
    dst_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("entities.id", ondelete="CASCADE", name="fk_relations_dst"), index=True
    )
    relation: Mapped[str] = mapped_column(String(30), default="OTHER")
    # 模型给的原始关系词：不在枚举内时 relation 存 OTHER，原词留在这里，
    # 攒够频次就能反推该往枚举里加什么（而不是拍脑袋定词表）
    raw_relation: Mapped[str] = mapped_column(String(30), default="")
    strength: Mapped[float] = mapped_column(Float, default=0.5)      # 关系强度 0-1，影响子图排序
    confidence: Mapped[float] = mapped_column(Float, default=0.0)    # 模型自评置信度
    status: Mapped[str] = mapped_column(String(10), default="active")  # active | pending（待人工确认）
    evidence: Mapped[str] = mapped_column(Text, default="")
    chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    chunk_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("chunks.id", ondelete="SET NULL", name="fk_relations_chunk"), default=None
    )
    created_at: Mapped[datetime] = _TZ.col(default=_now)

    __table_args__ = (
        # 同一对实体同一种关系只留一条；重跑靠 ON CONFLICT 更新证据与置信度
        UniqueConstraint("novel_id", "src_id", "dst_id", "relation", name="uq_relations_edge"),
        Index("ix_relations_novel_status", "novel_id", "status"),
    )


class EntityMention(Base):
    """实体在哪个块出现（章节-实体倒排）。

    两个用途：① 前端点实体看"在哪些章出现"；② RAG 的图谱增强靠它把
    "问题里提到的实体"映射回具体 chunk，再把这些 chunk 一起喂给生成。
    """

    __tablename__ = "entity_mentions"

    id: Mapped[str] = mapped_column(String(40), primary_key=True, default=lambda: new_id("men"))
    novel_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("novels.id", ondelete="CASCADE", name="fk_mentions_novel"), index=True
    )
    entity_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("entities.id", ondelete="CASCADE", name="fk_mentions_entity"), index=True
    )
    chapter_no: Mapped[int] = mapped_column(Integer, default=0)
    chunk_id: Mapped[str | None] = mapped_column(
        String(40), ForeignKey("chunks.id", ondelete="SET NULL", name="fk_mentions_chunk"), default=None
    )
    count: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        UniqueConstraint("entity_id", "chunk_id", name="uq_mentions_entity_chunk"),
        Index("ix_mentions_novel_chapter", "novel_id", "chapter_no"),
    )
