from app.services.ingestion import chunker, parser


def _chapter(text: str, no: int = 1):
    return parser.ChapterSpan(chapter_no=no, title=f"第{no}章", char_start=0, char_end=len(text), text=text)


def test_parent_child_ratio():
    text = "句子内容，用来测试切块。" * 400  # 约 4800 字
    pairs, _ = chunker.build_chunks(_chapter(text), parent_size=1400, child_size=400, overlap=60)
    assert len(pairs) >= 3
    for parent, children in pairs:
        assert children, "父块必须有子块"
        assert parent.token_est <= 1400 * 1.3
        for child in children:
            assert child.parent_key == parent.parent_key
            assert child.level == 1 and parent.level == 0


def test_child_offsets_within_parent():
    text = "第一句话。第二句话！第三句话？" * 200
    pairs, _ = chunker.build_chunks(_chapter(text), 1200, 300, 40)
    for parent, children in pairs:
        assert children[0].char_start >= parent.char_start
        assert children[-1].char_end <= parent.char_end


def test_overlap_creates_shared_text():
    text = "".join(f"第{i}句内容。" for i in range(200))
    pairs, _ = chunker.build_chunks(_chapter(text), 1400, 300, 60)
    children = [c for _, cs in pairs for c in cs]
    if len(children) >= 2:
        # 相邻子块应存在重叠区（overlap=60）
        a, b = children[0], children[1]
        assert b.char_start < a.char_end


def test_scene_separator_splits_parents():
    text = ("场景一的内容。" * 100) + "\n\n***\n\n" + ("场景二的内容。" * 100)
    pairs, _ = chunker.build_chunks(_chapter(text), 1400, 400, 60)
    assert len(pairs) >= 2


def test_token_estimate_cjk():
    assert chunker.estimate_tokens("中文字符") == 4
    assert chunker.estimate_tokens("abcd") == 2


def test_no_empty_chunks():
    pairs, _ = chunker.build_chunks(_chapter("短句。" * 50), 1400, 400, 60)
    for parent, children in pairs:
        assert parent.content.strip()
        for c in children:
            assert c.content.strip()
