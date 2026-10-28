"""对齐层的回归测试。

这组用例全部来自《三国演义》第一版图谱的真实事故：模型把「丞相」「将军」「主公」
写进 aliases，L3 顺着别名链把半本书焊成几个 blob，曹操（全书 941 次）从实体表消失。
每条断言都对应一条当时的失败现象，改规则时别把它们改回去。
"""

from app.services.graph.aligner import MAX_GROUP_SIZE, align
from app.services.graph.extractor import ExtractedEntity, parse_extraction
from app.services.graph.lexicon import canonical_score, is_honorific, is_mergeable_alias


def _ent(name: str, *aliases: str, etype: str = "Character") -> ExtractedEntity:
    return ExtractedEntity(name=name, type=etype, aliases=list(aliases))


# ---------- 词表 ----------


def test_honorific_detection():
    for w in ("丞相", "将军", "主公", "明公", "天子", "陛下", "大王", "恩相", "大人", "夫人"):
        assert is_honorific(w), w
    # 「前缀 + 泛称」两层组合
    assert is_honorific("老将军")
    assert is_honorific("众将军")
    # 专名不能被误杀：真人/先生是泛称，但带上名字就不是
    for w in ("孟德", "卧龙", "凤雏", "玄阳真人", "诸葛孔明", "阿瞒"):
        assert not is_honorific(w), w


def test_title_suffix_alias_is_not_mergeable():
    # 封号往往唯一指向某人，但模型会张冠李戴（把"汉中王"给陈留王），
    # 所以允许展示、不允许当合并依据
    assert not is_mergeable_alias("汉中王")
    assert not is_mergeable_alias("温侯")
    assert not is_mergeable_alias("汉献帝")
    # 字 / 号 / 别称可以
    assert is_mergeable_alias("孟德")
    assert is_mergeable_alias("玄德")
    assert is_mergeable_alias("卧龙")
    assert is_mergeable_alias("诸葛孔明")
    # 太短太长都不行
    assert not is_mergeable_alias("飞")
    assert not is_mergeable_alias("桃园三结义")


def test_canonical_score_prefers_real_name():
    # 本名 > 封号
    assert canonical_score("诸葛亮") > canonical_score("诸葛武侯")
    assert canonical_score("关羽") > canonical_score("汉寿亭侯")
    # 泛称排最后
    assert canonical_score("丞相") < 0
    assert canonical_score("曹操") > canonical_score("丞相")


# ---------- L2 抽取层 ----------


def test_extractor_drops_honorific_aliases():
    raw = {
        "entities": [
            {
                "name": "董卓",
                "type": "Character",
                "aliases": ["相国", "太师", "恩相", "丞相", "仲颖"],
                "desc": "",
            }
        ],
        "relations": [],
    }
    res = parse_extraction(raw, text="董卓入京，自领前将军。")
    assert len(res.entities) == 1
    assert res.entities[0].aliases == ["仲颖"]
    assert res.stats["dropped_honorific_alias"] == 4


# ---------- L3 对齐层 ----------


def test_honorific_does_not_bridge_entities():
    """第一版的核心事故：三个人因为共享别名「丞相」被焊成一个。"""
    ents = [
        _ent("曹操", "孟德", "丞相"),
        _ent("董卓", "相国", "丞相"),
        _ent("诸葛武侯", "丞相", "孔明"),
    ]
    result = align(ents)
    names = {e.name for e in result.entities}
    assert names == {"曹操", "董卓", "诸葛武侯"}
    assert not result.merged_pairs
    # 别名里也不该再出现泛称
    for e in result.entities:
        assert "丞相" not in e.aliases


def test_real_alias_merges():
    ents = [_ent("曹操", "孟德", "曹孟德"), _ent("孟德")]
    result = align(ents)
    assert len(result.entities) == 1
    only = result.entities[0]
    assert only.name == "曹操"
    assert "孟德" in only.aliases and "曹孟德" in only.aliases
    # 关系回填靠 alias_map，成员名一个都不能漏
    assert result.alias_map["孟德"] == "曹操"
    assert result.alias_map["曹孟德"] == "曹操"


def test_merge_chain_is_capped():
    """链式误并的极端形态：一串只有邻接关系的名字，不能被并成一个 blob。"""
    ents = [_ent(f"人物{i}", *(f"人物{i - 1}",) if i else ()) for i in range(12)]
    result = align(ents)
    assert len(result.entities) >= 12 // MAX_GROUP_SIZE
    for e in result.entities:
        assert len(e.aliases) + 1 <= MAX_GROUP_SIZE
    # 每个名字都还在某个实体的名下，关系不会因此断掉
    for i in range(12):
        assert result.alias_map.get(f"人物{i}") is not None


def test_member_names_survive_alias_cap():
    """曹操消失的直接原因是别名被 [:12] 截断，本名连别名都没保住。"""
    aliases = [f"别称{i}" for i in range(20)]
    ents = [_ent("诸葛武侯", *aliases, "孔明"), _ent("孔明"), _ent("诸葛亮", "卧龙")]
    result = align(ents)
    boss = next(e for e in result.entities if e.name == "诸葛武侯")
    assert "孔明" in boss.aliases
    assert len(boss.aliases) <= 16


def test_same_name_different_type_not_merged():
    ents = [
        _ent("许都", etype="Location"),
        ExtractedEntity(name="许都", type="Event", aliases=[]),
    ]
    result = align(ents)
    assert len(result.entities) == 2
