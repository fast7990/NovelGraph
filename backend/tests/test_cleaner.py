from app.services.ingestion import cleaner
from app.services.ingestion.cleaner import CleanerConfig


def cfg(**kw) -> CleanerConfig:
    base = dict(
        enabled=True, html_tags=True, html_entities=True, ads=True,
        page_numbers=True, repeated_lines=True, punctuation=True, max_repeat=2,
    )
    base.update(kw)
    return CleanerConfig(**base)


def test_html_tags_removed():
    t = "正文<p>段落</p>内容<br>换行"
    r = cleaner.clean(t, cfg())
    assert "<p>" not in r.text and "<br>" not in r.text
    assert r.stats.get("html_tags") == 3


def test_html_entities_decoded():
    t = "汤姆&amp;杰瑞&nbsp;说&hellip;"
    r = cleaner.clean(t, cfg())
    assert r.text == "汤姆&杰瑞 说…"
    assert r.stats.get("html_entities") == 3


def test_numeric_entity():
    t = "价格&#165;100"
    assert cleaner.clean(t, cfg()).text == "价格¥100"


def test_ad_lines_dropped():
    t = "第1章 开端\n\n求月票！求推荐票！\n\n他推开门走了进去。\n\n请收藏支持正版\n"
    r = cleaner.clean(t, cfg())
    assert "求月票" not in r.text
    assert "请收藏支持正版" not in r.text
    assert "他推开门走了进去" in r.text
    assert r.stats.get("ads") == 2


def test_ad_does_not_eat_prose():
    # "求他原谅" 不在广告词表里，不应被删
    t = "第1章 甲\n\n她终于求他原谅了。\n"
    r = cleaner.clean(t, cfg())
    assert "求他原谅" in r.text
    assert "ads" not in r.stats


def test_page_numbers_dropped():
    t = "第1章 甲\n\n正文一行。\n\n- 12 -\n\n第2章 乙\n\n第 3 页\n"
    r = cleaner.clean(t, cfg())
    assert "- 12 -" not in r.text
    assert "第 3 页" not in r.text
    assert r.stats.get("page_numbers") == 2


def test_repeated_lines_collapsed():
    t = "第1章 甲\n\n重复句。\n重复句。\n重复句。\n重复句。\n正常句。"
    r = cleaner.clean(t, cfg(max_repeat=2))
    # 保留 2 行，丢弃 2 行
    assert r.text.count("重复句。") == 2
    assert r.stats.get("repeated_lines") == 2


def test_punctuation_normalized_only_near_cjk():
    t = "他说:你好!这是测试?对,没错(是的)"
    r = cleaner.clean(t, cfg())
    assert r.text == "他说：你好！这是测试？对，没错（是的）"
    assert r.stats.get("punctuation") == 6


def test_punctuation_keeps_time_and_url():
    t = "会议12:30开始，详见http://a.b/c。"
    r = cleaner.clean(t, cfg())
    # 半角冒号在 ASCII 上下文不动；句末中文句号后无 CJK，不动
    assert "12:30" in r.text and "http://a.b/c" in r.text


def test_disabled_returns_unchanged():
    t = "求月票！<br>正文&nbsp;第 3 页"
    r = cleaner.clean(t, cfg(enabled=False))
    assert r.text == t
    assert r.stats == {}


def test_individual_toggles():
    t = "求月票！<br>正文"
    r = cleaner.clean(t, cfg(ads=False, html_tags=True))
    assert "求月票" in r.text        # 广告保留
    assert "<br>" not in r.text       # 标签仍清
    assert "ads" not in r.stats


def test_integration_parse_emits_clean_issue():
    from app.services.ingestion import parser

    raw = "第1章 甲\n\n求月票支持！\n\n正文内容。<br>测试&nbsp;完毕。\n\n第2章 乙\n\n短"
    result = parser.parse(raw.encode())
    codes = {i["code"] for i in result.issues}
    assert "CLEAN_ADS" in codes
    assert "CLEAN_HTML_TAGS" in codes
    assert "CLEAN_HTML_ENTITIES" in codes
    # 清洗后的文本才是规范文本，且不含噪声
    assert "<br>" not in result.text
    assert "求月票支持" not in result.text
    assert result.clean_stats.get("ads") == 1
