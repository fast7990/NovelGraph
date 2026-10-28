"""L3 对齐层：把一块块抽出来的名字合并成"一个人"。

同一人物在小说里有多个称谓（本名/字/号/绰号/官职），分块抽取时更容易分裂：
第 3 章只叫"孟德"，第 7 章只叫"曹操"，抽出来就是两个实体，子图里各挂一半关系。

合并只走两条**可验证**的路径，不做激进猜测：

1. **同名同类型** —— 天然合并，靠唯一键
2. **专名别名指向** —— A 的 aliases 里有 B 的名字，且 `is_mergeable_alias` 放行

第二道闸门是血的教训。《三国演义》第一版图谱里，模型把"丞相"写进了曹操、董卓、
诸葛武侯三个实体的 aliases，并查集顺着这根链一路 union，长出 2961 条提及的 blob，
曹操本人被 12 条别名上限挤掉，从实体表和搜索里彻底消失。所以现在：

- 泛称别名在进图前就被 `lexicon.is_honorific` 剔掉；
- 只有 2-4 字、非泛称、非封号结尾的专名才能触发合并；
- 单次合并有连通分量上限（`MAX_GROUP_SIZE`），宁可让一个人拆成两个节点，
  也不要把半本书焊成一个——前者看得见、能人工合，后者不可逆。

刻意**不做**的两件事：

- **字符相似度合并**。"诸葛亮"vs"诸葛孔明"编辑距离很远合不上，
  而"张飞"vs"张飞燕"很近却可能是两个人。这个方向收益低、误伤高。
- **短名包含长名合并**。中文小说里"曹操"和"曹操军"、"刘备"和"刘备麾下"
  是不同实体，包含关系反而最容易把势力并进人物。

真需要更强的对齐时，正确做法是拿实体名去向量库里比对描述相似度，
而不是在字符串上凑阈值。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from app.services.graph.extractor import ExtractedEntity
from app.services.graph.lexicon import canonical_score, is_honorific, is_mergeable_alias

# 一个连通分量最多合并几个实体。实测：正常情况下同一个人能被抽成 2-4 个名字
# （本名 / 字 / 号 / 别称），6 已经留了余量；超过就是"链式误并"的征兆。
MAX_GROUP_SIZE = 6

# 别名展示上限。成员本名排在最前面（构造时就是先放本名再放字号），
# 所以这个上限只会截掉边缘的绰号，不会把"曹孟德"这种关键信息挤掉。
ALIAS_KEEP = 16


@dataclass
class MergedEntity:
    name: str
    type: str
    aliases: list[str] = field(default_factory=list)
    desc: str = ""
    first_chapter: int = 0
    mention_count: int = 0

    @property
    def key(self) -> tuple[str, str]:
        return (self.type, self.name)


class UnionFind:
    def __init__(self) -> None:
        self.parent: dict[tuple[str, str], tuple[str, str]] = {}

    def add(self, k: tuple[str, str]) -> None:
        self.parent.setdefault(k, k)

    def find(self, k: tuple[str, str]) -> tuple[str, str]:
        self.add(k)
        root = k
        while self.parent[root] != root:
            root = self.parent[root]
        while self.parent[k] != root:      # 路径压缩
            self.parent[k], k = root, self.parent[k]
        return root

    def union(self, a: tuple[str, str], b: tuple[str, str]) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


@dataclass
class AlignResult:
    entities: list[MergedEntity]
    # 任意别名 → 规范名，回填关系时用它把 "孟德" 映射成 "曹操"
    alias_map: dict[str, str]
    # (组内主名, 被并入的名字, 触发合并的桥别名)。桥别名是审计用的：
    # 一旦再出现 blob，直接看是哪个词把两个不相干的人连起来的。
    merged_pairs: list[tuple[str, str, str]]

    def canonical(self, name: str, etype: str = "") -> str:
        if etype:
            return self.alias_map.get(f"{etype}\x00{name}") or self.alias_map.get(name) or name
        return self.alias_map.get(name) or name


def align(
    entities: list[ExtractedEntity],
    *,
    mentions: dict[str, int] | None = None,
    first_chapter: dict[str, int] | None = None,
) -> AlignResult:
    """合并同名与专名别名指向的实体。

    `mentions` / `first_chapter` 的 key 是 `f"{type}\x00{name}"`，
    由调用方在同一遍扫描里统计（避免这里再回表）。不传也能跑：
    此时 mention_count 退化成"这个词被多少块抽到"，用来当选规范名的 tie-break 已经够。
    """
    mentions = mentions or {}
    first_chapter = first_chapter or {}

    acc: dict[tuple[str, str], MergedEntity] = {}
    for e in entities:
        key = (e.type, e.name)
        m = acc.get(key)
        if m is None:
            m = MergedEntity(
                name=e.name,
                type=e.type,
                desc=e.desc,
                first_chapter=first_chapter.get(f"{e.type}\x00{e.name}", 0),
                mention_count=mentions.get(f"{e.type}\x00{e.name}", 0),
            )
            acc[key] = m
        else:
            # 同一实体被多块抽到：描述取最长的（信息量最大），章节取最早的
            if len(e.desc) > len(m.desc):
                m.desc = e.desc
            fc = first_chapter.get(f"{e.type}\x00{e.name}", 0)
            if fc and (not m.first_chapter or fc < m.first_chapter):
                m.first_chapter = fc
        # 被多少块抽到 = 这个词在书里有多常用，是选规范名时唯一可靠的 tie-break
        m.mention_count += 1
        for a in e.aliases:
            # L3 的第二道防线：即便 L2 那一层漏了（历史数据、别的调用方），
            # 泛称也不进别名表——它一进来就是合并的依据。
            if a != m.name and a not in m.aliases and not is_honorific(a):
                m.aliases.append(a)

    uf = UnionFind()
    size: dict[tuple[str, str], int] = {}
    for key in acc:
        uf.add(key)
        size[key] = 1

    # 候选合并对，按"证据强度"从高到低处理。强度用两边提及量的较小值：
    # 一个只出现一次的"孟德"不该把提及八百次的"曹操"拽过去；
    # 2 字专名比 3-4 字更可靠，给一点加成当 tie-break。
    cand: list[tuple[float, tuple[str, str], tuple[str, str], str]] = []
    for key, m in acc.items():
        for alias in m.aliases:
            if not is_mergeable_alias(alias, m.name):
                continue
            target = (m.type, alias)
            if target not in acc:
                continue
            other = acc[target]
            strength = min(m.mention_count, other.mention_count) + (0.5 if len(alias) == 2 else 0)
            cand.append((strength, key, target, alias))
    cand.sort(key=lambda x: -x[0])

    merged_pairs: list[tuple[str, str, str]] = []
    for _strength, ka, kb, alias in cand:
        ra, rb = uf.find(ka), uf.find(kb)
        if ra == rb:
            continue
        if size[ra] + size[rb] > MAX_GROUP_SIZE:
            # 再并下去就是 blob：曹操 —丞相→ 董卓 —丞相→ 诸葛武侯 这种链式误并。
            continue
        if size[ra] < size[rb]:          # 大组当根，最终规范名更稳
            ra, rb = rb, ra
        uf.parent[rb] = ra
        size[ra] += size[rb]
        merged_pairs.append((acc[ra].name, acc[rb].name, alias))

    groups: dict[tuple[str, str], list[MergedEntity]] = {}
    for key, m in acc.items():
        groups.setdefault(uf.find(key), []).append(m)

    merged: list[MergedEntity] = []
    alias_map: dict[str, str] = {}
    for members in groups.values():
        # 规范名：先看字面结构（本名 > 字 > 号 > 封号），提及量只做 tie-break。
        # 不能反过来——提及量本身被泛称灌过水，拿污染源当裁判没有意义。
        members.sort(key=lambda x: (-canonical_score(x.name), -x.mention_count, -len(x.name)))
        head = members[0]

        total = 0
        first = head.first_chapter
        desc = head.desc
        for other in members:
            total += other.mention_count
            if other.first_chapter and (not first or other.first_chapter < first):
                first = other.first_chapter
            if len(other.desc) > len(desc):
                desc = other.desc

        # 别名顺序：先成员本名（"曹孟德"比任何绰号都重要），再各成员的字/号
        aliases: list[str] = []
        for other in members:
            if other is not head and other.name != head.name and other.name not in aliases:
                aliases.append(other.name)
        for other in members:
            for a in other.aliases:
                if a != head.name and a not in aliases:
                    aliases.append(a)

        merged.append(
            MergedEntity(
                name=head.name,
                type=head.type,
                aliases=aliases,
                desc=desc,
                first_chapter=first,
                mention_count=total,
            )
        )
        # 成员名一律映射到 head，**不做别名截断**：
        # 这里漏一个名字，回填关系时就少一条边（第一版曹操消失的直接原因）。
        for member in members:
            alias_map[member.name] = head.name
            alias_map[f"{head.type}\x00{member.name}"] = head.name

    merged.sort(key=lambda x: -x.mention_count)

    # 一个词只能归一个人：别名撞车时归给提及量最大的那个组。
    # 本名永远优先——它是另一条实体的 name，不能被抢去当别名。
    all_names = {(e.type, e.name) for e in merged}
    claimed: dict[tuple[str, str], str] = {}
    for ent in merged:
        keep: list[str] = []
        for a in ent.aliases:
            if a == ent.name or (ent.type, a) in all_names:
                continue
            k = (ent.type, a)
            if k in claimed:
                continue
            claimed[k] = ent.name
            keep.append(a)
        ent.aliases = keep[:ALIAS_KEEP]
        for a in ent.aliases:
            alias_map[a] = ent.name
            alias_map[f"{ent.type}\x00{a}"] = ent.name

    return AlignResult(entities=merged, alias_map=alias_map, merged_pairs=merged_pairs)
