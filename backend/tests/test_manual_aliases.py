"""人工别名表的读写测试。

这张表是"规则合不上、只能靠知识指定"的出口，写坏了会让整本书的图谱对齐
跑偏（v1.10 的 blob 事故就是别名表失控）。所以它的合并语义（default 打底、
书级覆盖、自映射剔除）和写回格式都要钉住。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.graph import manual_aliases  # noqa: E402


def _write(p: Path, data) -> Path:
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def test_missing_file_returns_empty(tmp_path):
    assert manual_aliases.aliases_for("nv_x", tmp_path / "nope.json") == {}


def test_broken_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("{ 这不是 json", encoding="utf-8")
    # 配置坏了要让图谱照常能建，不能因为一行手滑把建图流程整个炸掉
    assert manual_aliases.aliases_for("nv_x", p) == {}


def test_default_then_novel_override(tmp_path):
    p = _write(
        tmp_path / "a.json",
        {
            "default": {"武侯": "诸葛亮", "子龙": "赵云"},
            "nv_1": {"子龙": "常山赵子龙"},
        },
    )
    out = manual_aliases.aliases_for("nv_1", p)
    assert out["武侯"] == "诸葛亮"       # default 打底
    assert out["子龙"] == "常山赵子龙"   # 书级覆盖
    # 别的书拿不到书级条目
    assert manual_aliases.aliases_for("nv_2", p)["子龙"] == "赵云"


def test_self_mapping_dropped(tmp_path):
    p = _write(tmp_path / "a.json", {"default": {"诸葛亮": "诸葛亮"}})
    # 自映射没有意义，留着只会在 align 里多绕一圈
    assert manual_aliases.aliases_for("nv_x", p) == {}


def test_readme_key_ignored(tmp_path):
    p = _write(tmp_path / "a.json", {"_README": "说明文字", "default": {"武侯": "诸葛亮"}})
    # 配置文件里带说明键是给人看的，不能被当成书 ID
    assert manual_aliases.aliases_for("_README", p) == {"武侯": "诸葛亮"}


def test_upsert_preserves_other_entries(tmp_path):
    p = _write(tmp_path / "a.json", {"default": {"武侯": "诸葛亮"}, "nv_1": {"献帝": "汉献帝"}})
    manual_aliases.upsert("nv_1", "陈留王", "汉献帝", p)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert data["default"]["武侯"] == "诸葛亮"
    assert data["nv_1"]["陈留王"] == "汉献帝"
    assert data["nv_1"]["献帝"] == "汉献帝"
    # 中文必须保持可读（ensure_ascii=False），否则这个文件就没法手工编辑了
    assert "陈留王" in p.read_text(encoding="utf-8")
