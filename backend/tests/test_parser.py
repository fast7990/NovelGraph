import pytest

from app.services.ingestion import parser


def test_decode_gbk():
    text = "第1章 雨夜\n\n陈默推开门，屋里一片漆黑。"
    raw = text.encode("gb18030")
    decoded, enc = parser.decode_bytes(raw)
    assert "陈默推开门" in decoded
    assert enc in ("gb18030", "gbk", "cp949")  # charset-normalizer 可能报 gbk 变体


def test_decode_utf8_with_bom():
    raw = b"\xef\xbb\xbf" + "第1章 测试\n\n正文内容。".encode("utf-8")
    decoded, enc = parser.decode_bytes(raw)
    assert decoded.startswith("第1章")
    assert enc == "utf-8-sig"


def test_split_chapters_basic():
    raw = "\n".join(f"第{i}章 标题{i}\n\n" + ("正文内容。" * 60) for i in range(1, 6))
    result = parser.parse(raw.encode("utf-8"))
    assert len(result.chapters) == 5
    assert result.chapters[0].chapter_no == 1
    assert result.chapters[0].char_start == 0
    # 区间连续，不重叠
    for a, b in zip(result.chapters, result.chapters[1:]):
        assert a.char_end == b.char_start


def test_split_chapters_fallback_when_no_structure():
    raw = "这是一段没有章节标题的长文本。" * 1000
    result = parser.parse(raw.encode("utf-8"))
    assert len(result.chapters) > 1
    assert any(i["code"] == "NO_CHAPTER_STRUCTURE" for i in result.issues)


def test_short_chapter_flagged():
    body = "第1章 甲\n\n" + "正文。" * 100
    body += "\n第2章 乙\n\n短\n第3章 丙\n\n" + "正文。" * 100
    result = parser.parse(body.encode("utf-8"))
    assert any(i["code"] == "CHAPTER_TOO_SHORT" for i in result.issues)


def test_offsets_point_into_text():
    raw = "第1章 甲\n\n" + "林薇薇找到了纽扣。" * 60 + "\n第2章 乙\n\n" + "陈默离开了码头。" * 60
    result = parser.parse(raw.encode("utf-8"))
    for ch in result.chapters:
        assert result.text[ch.char_start : ch.char_start + 3].startswith("第")
        assert ch.text[:4] in result.text[ch.char_start : ch.char_end + 10]


@pytest.mark.parametrize("size", [200, 400, 1400])
def test_normalize_strips_blank_runs(size):
    raw = "第1章 甲\n\n\n\n" + ("句子。" * size) + "\n\n\n\n"
    result = parser.parse(raw.encode("utf-8"))
    assert "\n\n\n" not in result.text
