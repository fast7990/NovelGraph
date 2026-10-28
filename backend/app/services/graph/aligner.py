"""L3 对齐层：把一块块抽出来的名字合并成"一个人"。

同一人物在小说里有多个称谓（本名/字/号/绰号/官职），分块抽取时更容易分裂：
第 3 章只叫"孟德"，第 7 章只叫"曹操"，抽出来就是两个实体，子图里各挂一半关系。

合并只走两条**可验证**的路径，不做激进猜测：

1. **同名同类型** —— 天然合并，靠唯一键
2. **别名指向** —— A 的 aliases 里有 B 的名字，且同类型，就并到 A

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
    merged_pairs: list[tuple[str, str]]

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
    """合并同名与别名指向的实体。

    `mentions` / `first_chapter` 的 key 是 `f"{type}\x00{name}"`，
    由调用方在同一遍扫描里统计（避免这里再回表）。
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
                mention_count=mentions.get(f"{e.type}\x00{e.name}", 1),
            )
            acc[key] = m
        else:
            # 同一实体被多块抽到：描述取最长的（信息量最大），章节取最早的
            if len(e.desc) > len(m.desc):
                m.desc = e.desc
            fc = first_chapter.get(f"{e.type}\x00{e.name}", 0)
            if fc and (not m.first_chapter or fc < m.first_chapter):
                m.first_chapter = fc
        for a in e.aliases:
            if a != m.name and a not in m.aliases:
                m.aliases.append(a)

    uf = UnionFind()
    for key in acc:
        uf.add(key)

    # 路径 2：别名指向已有实体名 → 合并。
    # 只在同类型内做，且"指向方"必须比"被指向方"提及更少，
    # 否则会出现"曹操"被并进"孟德"这种反直觉结果（谁常用谁是主名）。
    merged_pairs: list[tuple[str, str]] = []
    for key, m in acc.items():
        for alias in m.aliases:
            target = (m.type, alias)
            if target not in acc:
                continue
            other = acc[target]
            if other.mention_count > m.mention_count:
                uf.union(target, key)
                merged_pairs.append((other.name, m.name))
            else:
                uf.union(key, target)
                merged_pairs.append((m.name, other.name))

    groups: dict[tuple[str, str], list[MergedEntity]] = {}
    for key, m in acc.items():
        groups.setdefault(uf.find(key), []).append(m)

    merged: list[MergedEntity] = []
    alias_map: dict[str, str] = {}
    for members in groups.values():
        # 规范名：提及最多的那个。并列时取更长的（"诸葛孔明" 比 "孔明" 信息量大）
        members.sort(key=lambda x: (-x.mention_count, -len(x.name)))
        head = members[0]
        alias_set: set[str] = set(head.aliases)
        total = head.mention_count
        first = head.first_chapter
        desc = head.desc
        for other in members[1:]:
            alias_set.add(other.name)
            # 被合并方的别名也是这个人的别名（"孟德"的别名"阿瞒"）
            alias_set.update(other.aliases)
            total += other.mention_count
            if other.first_chapter and (not first or other.first_chapter < first):
                first = other.first_chapter
            if len(other.desc) > len(desc):
                desc = other.desc

        merged_entity = MergedEntity(
            name=head.name,
            type=head.type,
            aliases=[a for a in alias_set if a != head.name][:12],
            desc=desc,
            first_chapter=first,
            mention_count=total,
        )
        merged.append(merged_entity)
        for name in [head.name, *merged_entity.aliases]:
            alias_map[name] = head.name
            alias_map[f"{head.type}\x00{name}"] = head.name

    merged.sort(key=lambda x: -x.mention_count)
    return AlignResult(entities=merged, alias_map=alias_map, merged_pairs=merged_pairs)
