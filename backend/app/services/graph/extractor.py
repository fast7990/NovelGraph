"""L2 抽取层：把一块正文交给 LLM，拿回实体与关系，并当场过三道质量闸门。

设计要点：

**按块抽而不是按章抽**。一本百万字的书一章可能几万字，一次调用塞不下
（输出 JSON 会长到不可控），而且模型在长上下文里对"某条关系的证据在哪"
会开始编。按 parent 块（800-1400 字）抽，token 预算可控、证据可定位。

**闸门放在这一层，不放后面**。三个检查都需要块原文（evidence 校验）和
关系词表（枚举归拢），离开这里就得把原文再传一遍。

**置信度低的不丢，标 pending**。模型自评 0.55 的关系里有一半是对的，
直接丢弃等于白烧 token；标成待确认，管理台可以人工过一遍，
这比"宁可错杀"对图谱覆盖率友好得多。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from app.config import settings
from app.models import ENTITY_TYPES, RELATION_TYPES
from app.services.graph.lexicon import is_honorific

logger = logging.getLogger(__name__)

# 低于这个置信度入 pending 队列（人工审核），高于才直接 active
CONFIDENCE_ACTIVE = settings.graph_confidence_active

_WS = re.compile(r"\s+")

SYSTEM_PROMPT = """你是中文小说的知识图谱抽取器。从给定正文中抽取实体与关系，只输出 JSON。

## 实体类型（type 只能取这六个）
- Character 人物（含称谓、绰号）
- Faction 势力、门派、家族、朝廷、军队
- Location 地点、城池、山川、建筑
- Item 物品、功法、法宝、兵器、药材
- Event 事件（发生过的具体事情）
- Concept 设定、术语、境界、官职、制度

## 关系词（relation 只能从以下选，没有合适的用 OTHER）
师徒 父子 母子 父女 母女 兄弟 姐妹 夫妻 道侣 爱慕 敌对 盟友 上下级 同门 朋友 主仆 亲戚 归属 参与 位于 持有 OTHER

## 抽取规则
1. 只抽正文里明确写出的内容。不要用你的常识补充正文没提的关系。
2. evidence 必须逐字复制正文里的连续片段（10-60 字），不要改写、不要加省略号、不要自己总结。这一条最重要：找不到原文出处的关系统计上多半是错的。
3. 同一人物的不同称谓放进 aliases，但**只填专名**：字（孟德）、号（卧龙）、小名（阿瞒）、绰号（凤雏）、别称（诸葛孔明）。name 用正文里最常用的那个。
   **严禁**把官职、尊称、泛称写进 aliases —— 丞相、相国、太师、将军、都督、太守、主公、明公、恩相、大人、先生、夫人、公子、天子、陛下、大王、殿下 这类称呼在全书里指向很多人，混进别名会把不同的人合并成一个，是图谱最主要的污染源。
4. 不抽代词（他/那人/此人/众人）。只出现一次且没有任何关系的路人也不抽。
5. 关系的 src 和 dst 必须是你本次抽出的实体名，不能凭空造名字。
6. strength 是关系强度 0-1（决定子图里边的粗细）；confidence 是你对这条关系确实成立的把握 0-1。证据含糊、需要推断时 confidence 必须低于 0.6。
7. 事件也当实体抽：name 用简短动宾短语（如"桃园结义""三顾茅庐"），涉及人物用"参与"连到事件。
8. desc 一句话、不超过 30 字，只写正文支持的信息；说不出来就留空串。

## 取舍原则（比"多抽"更重要）
9. **人物之间的关系是重点**。"谁跟谁是什么关系"决定了这张图有没有用；
   "谁站在哪""谁手里拿着什么"这类结构信息价值低得多。
10. **物品只抽关键道具**：信物、凶器、证物、法宝、秘籍、决定剧情走向的物件。
    桌椅、茶具、便条、行李、饭菜这类日常器物一律不抽，除非正文点明它就是线索本身。
11. **地点只抽有情节发生的地点**，不抽泛指的区域（"城里""远处""山上"）。
12. 一块正文里如果只能抽出人-物、人-地关系而没有人与人的关系，就少抽几条，
    不要为了凑数量把"某人把某物放在桌上"全列成关系。
13. **控制输出规模**：每块最多 10 个实体、12 条关系。宁可漏掉次要的，
    也不要让 JSON 长到被截断——截断的 JSON 整块作废，一条都用不上。

## 输出格式
{"entities":[{"name":"","type":"","aliases":[],"desc":""}],
 "relations":[{"src":"","dst":"","relation":"","strength":0.8,"confidence":0.9,"evidence":""}]}

## 示例
正文：却说张飞字翼德，世居涿郡，颇有庄田，卖酒屠猪。玄德看其人，身长八尺。飞曰：“吾颇有资财，当招募乡勇，与公同举大事。”
输出：{"entities":[{"name":"张飞","type":"Character","aliases":["翼德"],"desc":"涿郡人，卖酒屠猪"},{"name":"刘备","type":"Character","aliases":["玄德"],"desc":"身长八尺"},{"name":"涿郡","type":"Location","aliases":[],"desc":""}],
"relations":[{"src":"张飞","dst":"涿郡","relation":"位于","strength":0.7,"confidence":0.9,"evidence":"张飞字翼德，世居涿郡"},{"src":"张飞","dst":"刘备","relation":"盟友","strength":0.8,"confidence":0.85,"evidence":"当招募乡勇，与公同举大事"}]}
"""


@dataclass
class ExtractedEntity:
    name: str
    type: str
    aliases: list[str] = field(default_factory=list)
    desc: str = ""


@dataclass
class ExtractedRelation:
    src: str
    dst: str
    relation: str
    raw_relation: str
    strength: float
    confidence: float
    evidence: str
    status: str = "active"
    # 由编排层在合并时回填：证据回链到具体块，前端点边能直接看原文
    chunk_id: str = ""
    chapter_no: int = 0


@dataclass
class ChunkExtraction:
    entities: list[ExtractedEntity] = field(default_factory=list)
    relations: list[ExtractedRelation] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def _norm(s: str) -> str:
    """归一化到"只留文字"，用于 evidence 与原文比对。

    空白、换行、全半角空格在复制粘贴里最容易被改，这些差异不该算编造；
    但标点和字词不动，动了就说明模型在改写。
    """
    return _WS.sub("", s or "")


def _clean_name(name: object) -> str:
    s = str(name or "").strip()
    return s[:120]


def parse_extraction(raw: dict, *, text: str, known: set[str] | None = None) -> ChunkExtraction:
    """把模型返回的原始 JSON 转成结构体，并在这一层执行三道闸门。"""
    stats = {
        "raw_entities": 0,
        "raw_relations": 0,
        "entities": 0,
        "relations": 0,
        "dropped_bad_entity": 0,
        "dropped_honorific_alias": 0,
        "dropped_bad_relation": 0,
        "dropped_no_evidence": 0,
        "evidence_partial": 0,
        "pending": 0,
        "other_relation": 0,
    }

    body = _norm(text)
    entities: list[ExtractedEntity] = []
    seen: set[tuple[str, str]] = set()

    for item in raw.get("entities") or []:
        if not isinstance(item, dict):
            continue
        stats["raw_entities"] += 1
        name = _clean_name(item.get("name"))
        etype = str(item.get("type") or "").strip()
        if not name or etype not in ENTITY_TYPES:
            stats["dropped_bad_entity"] += 1
            continue
        key = (etype, name)
        if key in seen:
            continue
        seen.add(key)
        aliases = [
            _clean_name(a)
            for a in (item.get("aliases") or [])
            if isinstance(a, (str, int)) and 1 < len(_clean_name(a)) <= 20
        ]
        # 泛称别名是"两个实体被焊成一个"的唯一入口，在进图层之前就掐掉。
        # 提示词里已经禁过一遍，这里再按词表兜一层——模型不听话是常态。
        uniq = [a for a in dict.fromkeys(aliases) if a != name]
        kept = [a for a in uniq if not is_honorific(a)]
        stats["dropped_honorific_alias"] += len(uniq) - len(kept)
        entities.append(
            ExtractedEntity(
                name=name,
                type=etype,
                # 剔除和本名重复的别名，前端展示时不用再判重
                aliases=kept[:8],
                desc=str(item.get("desc") or "").strip()[:200],
            )
        )
    stats["entities"] = len(entities)

    names = {e.name for e in entities}
    if known:
        names |= known

    relations: list[ExtractedRelation] = []
    for item in raw.get("relations") or []:
        if not isinstance(item, dict):
            continue
        stats["raw_relations"] += 1
        src = _clean_name(item.get("src"))
        dst = _clean_name(item.get("dst"))
        if not src or not dst or src == dst or src not in names or dst not in names:
            stats["dropped_bad_relation"] += 1
            continue

        evidence = str(item.get("evidence") or "").strip()
        hit = _evidence_hit(evidence, body)
        if hit == "none":
            stats["dropped_no_evidence"] += 1
            continue
        if hit == "partial":
            stats["evidence_partial"] += 1

        raw_rel = str(item.get("relation") or "").strip() or "OTHER"
        relation = raw_rel if raw_rel in RELATION_TYPES else "OTHER"
        if relation == "OTHER" and raw_rel != "OTHER":
            stats["other_relation"] += 1

        confidence = _clamp(item.get("confidence"), 0.5)
        if hit == "partial":
            # 只命中前半段说明模型至少改写了后半段，把握要打折
            confidence = round(confidence * 0.9, 3)

        strength = _clamp(item.get("strength"), 0.5)
        status = "active" if confidence >= CONFIDENCE_ACTIVE else "pending"
        if status == "pending":
            stats["pending"] += 1

        relations.append(
            ExtractedRelation(
                src=src,
                dst=dst,
                relation=relation,
                raw_relation=raw_rel if relation == "OTHER" else "",
                strength=strength,
                confidence=confidence,
                evidence=evidence[:400],
                status=status,
            )
        )
    stats["relations"] = len(relations)

    return ChunkExtraction(entities=entities, relations=relations, stats=stats)


# 证据分句用：中文标点切开后逐段校验
_SENT_SPLIT = re.compile(r"[。！？；，、：\u201c\u201d\u2018\u2019]")


def _evidence_hit(evidence: str, body: str) -> str:
    """校验证据是否真出自原文，返回 exact / partial / none。

    为什么不能只做"整串 in body"：模型很爱把两句话拼成一条证据
    （`陈默在凌晨接到苏晚的电话。沈鹤站在城南河滩`），
    前半句抄对了、后半句是按人名套模板编的。整串比对能拦住，
    但只截前 16 字比对就一定会放行——正好是错得最隐蔽那种。

    所以先按标点切成小段，要求**每一段**都在原文里；
    有 60% 以上段落命中才算 partial（降权 0.9 并计数），
    命中率更低的直接丢弃。这样一个三段句里混一句编造的就过不去。
    """
    ev = _norm(evidence)
    if len(ev) < 6:
        return "none"
    if ev in body:
        return "exact"

    parts = [p for p in _SENT_SPLIT.split(ev) if len(p) >= 4]
    if parts:
        hits = sum(1 for p in parts if p in body)
        if hits == len(parts):
            # 段段都在，只是标点被改过（模型常把逗号补成句号）
            return "exact"
        if hits / len(parts) >= 0.6:
            return "partial"
        return "none"

    # 没有可切的小段（整条证据不分句）：退到前半段比对
    head = ev[: max(12, len(ev) * 2 // 3)]
    if head in body:
        return "partial"
    return "none"


def _clamp(value: object, default: float) -> float:
    try:
        v = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    return round(min(1.0, max(0.0, v)), 3)


async def extract_chunk(
    llm,  # noqa: ANN001  LLMClient，避免循环导入不写类型
    text: str,
    *,
    chapter_no: int = 0,
    chapter_title: str = "",
    known_names: list[str] | None = None,
) -> ChunkExtraction:
    """抽一块。LLM 失败时返回空结果而不是抛错——图谱是增强项，
    一块抽不出来不该让整本书的图谱构建失败。"""
    if not text.strip():
        return ChunkExtraction(stats={"empty": 1})

    prompt = f"第 {chapter_no} 章《{chapter_title}》正文：\n{text}"
    if known_names:
        # 只给 30 个：给太多模型会倾向于把提示里的人名硬套进正文
        names = "、".join(known_names[:30])
        prompt += f"\n\n（本章已确认的人名，仅用于统一称谓，不要引入正文没出现的：{names}）"

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    # 用 fast 模型：结构化抽取不需要长思考，而推理模型会把 max_tokens 全花在
    # reasoning_content 上、正文返回空串（实测 glm-5 抽一块返回 0 实体 0 关系）。
    # LLM_MODEL_FAST 未配置时退回主模型，并在结果为空时给出明确告警。
    model = llm.fast_model
    try:
        raw = await llm.complete_json(messages, temperature=0.0, max_tokens=4500, model=model)
    except Exception:  # noqa: BLE001
        logger.warning("图谱抽取失败 chapter=%s len=%s", chapter_no, len(text), exc_info=True)
        return ChunkExtraction(stats={"llm_error": 1})

    retried = False
    if not raw:
        # 最常见的原因是输出被截断（《三国演义》有些块 6000+ 字符的 JSON），
        # 截断后括号配平也救不回来。重试时明确要求精简，比直接放弃划算得多：
        # 这一块里通常有一两条关键关系，丢掉就是图谱上的一个洞。
        retried = True
        retry_prompt = (
            prompt
            + "\n\n注意：上一次输出无法解析（很可能是被截断）。这次只输出最关键的内容："
            "最多 6 个实体、6 条关系，确保 JSON 完整闭合。"
        )
        try:
            raw = await llm.complete_json(
                [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": retry_prompt}],
                temperature=0.0,
                max_tokens=4500,
                model=model,
            )
        except Exception:  # noqa: BLE001
            logger.warning("图谱抽取重试失败 chapter=%s", chapter_no, exc_info=True)

    if not raw:
        logger.warning(
            "图谱抽取返回空 JSON（model=%s, chapter=%s）。若主模型是推理模型，请配置 LLM_MODEL_FAST",
            model,
            chapter_no,
        )
        return ChunkExtraction(stats={"empty_json": 1, "retried": int(retried)})

    result = parse_extraction(raw, text=text)
    if retried:
        result.stats["retried"] = 1
    return result
