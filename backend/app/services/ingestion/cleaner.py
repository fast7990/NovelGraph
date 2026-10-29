"""正文清洗：在 normalize 之后、分章之前对整篇文本做内容级清洗。

为什么放在这里
- normalize() 只做"格式归一"（编码、BOM、空字节、换行、多余空行），不碰内容语义；
- 分章之后每个 chunk 是局部片段，清洗规则（广告行、页码、连续重复行）依赖"整行"判断，
  必须在分章前、对整篇文本做；
- 清洗后的文本才是规范文本，原样回写存储、作为所有 char_start/char_end 的唯一事实来源，
  下游切片与前端跳转定位都不用改。

所有规则都可单独关闭（见 config.py 的 clean_* 开关），命中计数进 ParseResult.clean_stats，
并在解析阶段生成 CLEAN_* 的 ChunkReport 条目，上传后能在"处理报告"里看到清掉了什么。

注意：清洗是启发式的，目标是干掉网文 txt 里最典型的噪声（广告/求票/页码/HTML 残留/复制重复）。
误伤风险靠"整行且短""相邻 CJK 才转换标点"等约束压到最低；真遇到误伤，关掉对应开关即可。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass
class CleanerConfig:
    enabled: bool = True
    html_tags: bool = True
    html_entities: bool = True
    ads: bool = True
    page_numbers: bool = True
    repeated_lines: bool = True
    punctuation: bool = True
    max_repeat: int = 2  # 连续相同行保留到这个数量，超出丢弃


@dataclass
class CleanResult:
    text: str
    stats: dict[str, int] = field(default_factory=dict)


# 规则 → 处理报告里的中文说明
CLEAN_DESC: dict[str, str] = {
    "html_tags": "HTML 标签",
    "html_entities": "HTML 转义实体",
    "ads": "广告 / 求票语",
    "page_numbers": "页码 / 分页符",
    "repeated_lines": "连续重复行",
    "punctuation": "半角标点转全角",
}


# ---------- 各规则的实现 ----------

# 控制字符（保留 \n \t），来自复制粘贴的乱码
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")

# HTML 标签与实体
_HTML_TAG = re.compile(r"<[^>]+>")
_HTML_ENTITY = re.compile(r"&(?:#x[0-9a-fA-F]+|#[0-9]+|[a-zA-Z][a-zA-Z0-9]*);")
_ENTITY_MAP = {
    "nbsp": " ", "amp": "&", "lt": "<", "gt": ">", "quot": '"', "apos": "'",
    "copy": "©", "reg": "®", "trade": "™", "hellip": "…", "middot": "·",
    "mdash": "—", "ndash": "–", "lsquo": "‘", "rsquo": "’", "ldquo": "“",
    "rdquo": "”", "bull": "•", "deg": "°", "times": "×", "divide": "÷",
    "plusmn": "±", "frac12": "½", "euro": "€", "pound": "£", "yen": "¥",
    "laquo": "«", "raquo": "»", "sect": "§", "para": "¶", "dagger": "†",
    "Dagger": "‡", "permil": "‰", "alpha": "α", "beta": "β", "gamma": "γ",
}

# 广告短语：行首锚定，逐个消费。设计为"强匹配"——必须整行基本由这些词构成，
# 避免误删正文里偶然出现的"月票"二字。"求月票！求推荐票！"这类并列整行也能整行判掉。
_AD_PHRASE = re.compile(
    r"^(?:"
    r"求月票支持"  # 组合短语放最前，避免被 "求月票" 抢先匹配后剩下 "支持" 消费不掉
    r"|(?:求|请)?(?:月票|推荐票|收藏|订阅|打赏|鲜花|评价票|追读|包养|盖饭|平安符|地雷|营养液)"
    r"|本书首发[^\n]{0,30}"
    r"|手机用户请访问[^\n]{0,40}"
    r"|请登录[^\n]{0,20}?(?:阅读)?"
    r"|会员[^\n]{0,20}?阅读"
    r"|支持正版|请支持正版"
    r"|[一-龥]{0,4}(?:小说网|文学网|看书网|阅读网|中文网)[^\n]{0,20}?"
    r"|本章说|作者有话要说|作者的话|说点什么|章说"
    r"|求[一-龥]{0,4}(?:票|收藏|订阅|打赏)"
    r"|(?:PS|P\.S\.|Ps|ps)[：:.\s].{0,40}"
    r")"
)
# 广告行里常见的连接/收尾标点，消费掉以便继续匹配下一个广告短语
_AD_SEP = re.compile(r"^[\s！!?。.…，,;:、~～—\-_|]+")

# 页码 / 分页符（独立成行才判，正文里的数字不误伤）
_PAGE_NUM = re.compile(
    r"^\s*(?:"
    r"\d[\d,\.]*\s*(?:页|/|／)?\s*"
    r"|\(\s*\d+\s*\)\s*"
    r"|-\s*\d+\s*-"
    r"|第\s*\d+\s*页"
    r")\s*$"
)

# 半角标点 → 全角（仅当紧邻 CJK 字符时），避免动到 "12:30" "http://" 等
_PUNCT_MAP = {",": "，", ";": "；", ":": "：", "!": "！", "?": "？", "(": "（", ")": "）"}
_CJK = r"\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff"
_PUNCT_LEFT = re.compile(rf"([{_CJK}])\s*([,;:!?()])\s*")
_PUNCT_RIGHT = re.compile(rf"\s*([,;:!?()])\s*([{_CJK}])")


def _decode_entity(m: re.Match) -> str:
    inner = m.group(0)[1:-1]
    if inner.startswith("#"):
        try:
            cp = int(inner[2:], 16) if inner[1] in "xX" else int(inner[1:])
            return chr(cp)
        except (ValueError, OverflowError):
            return m.group(0)
    return _ENTITY_MAP.get(inner.lower(), m.group(0))


def _is_ad(line: str) -> bool:
    """判断一行是否为广告/求票/站外引流等整行噪声。

    策略：循环消费——先试广告短语，再试标点/空白分隔，直到整行被消耗完。
    这样既支持「求月票！求推荐票！」这类并列整行，又靠「整行必须被吃完」避免误伤正文。
    """
    if not line or len(line) > 40:
        return False
    rest = line
    consumed = 0
    while True:
        m = _AD_PHRASE.match(rest)
        if m:
            rest = rest[m.end():]
            consumed += 1
            continue
        sep = _AD_SEP.match(rest)
        if sep:
            rest = rest[sep.end():]
            continue
        break
    return consumed >= 1 and rest == ""


def _punct_left(m: re.Match) -> str:
    return m.group(1) + _PUNCT_MAP[m.group(2)]


def _punct_right(m: re.Match) -> str:
    return _PUNCT_MAP[m.group(1)] + m.group(2)


def from_settings() -> CleanerConfig:
    """从全局配置读取开关（避免模块级 import 配置造成循环）。"""
    from app.config import settings

    return CleanerConfig(
        enabled=settings.clean_enabled,
        html_tags=settings.clean_html_tags,
        html_entities=settings.clean_html_entities,
        ads=settings.clean_ads,
        page_numbers=settings.clean_page_numbers,
        repeated_lines=settings.clean_repeated_lines,
        punctuation=settings.clean_punctuation,
        max_repeat=settings.clean_max_repeat,
    )


def clean(text: str, cfg: CleanerConfig | None = None) -> CleanResult:
    """对整篇规范化文本做内容清洗，返回清洗后文本与各项命中计数。"""
    if cfg is None:
        cfg = from_settings()
    if not cfg.enabled:
        return CleanResult(text=text, stats={})

    stats: dict[str, int] = {}
    t = text

    # 1) 控制字符（复制粘贴带来的乱码）
    t = _CTRL.sub("", t)

    # 2) HTML 标签
    if cfg.html_tags:
        tags = _HTML_TAG.findall(t)
        if tags:
            t = _HTML_TAG.sub("", t)
            stats["html_tags"] = len(tags)

    # 3) HTML 转义实体
    if cfg.html_entities and _HTML_ENTITY.search(t):
        t, n = _HTML_ENTITY.subn(_decode_entity, t)
        if n:
            stats["html_entities"] = n

    # 4) 全角空格 + 多空格折叠
    t = t.replace("　", " ")
    t = re.sub(r"[ \t]{2,}", " ", t)

    # 5) 行级：广告行 / 页码 / 连续重复行
    lines = t.split("\n")
    out: list[str] = []
    prev: str | None = None
    run = 0
    ad_n = page_n = rep_n = 0
    for ln in lines:
        s = ln.strip()
        if not s:
            out.append(ln)
            prev = None
            run = 0
            continue
        if cfg.ads and _is_ad(s):
            ad_n += 1
            prev = None
            run = 0
            continue
        if cfg.page_numbers and _PAGE_NUM.match(ln):
            page_n += 1
            prev = None
            run = 0
            continue
        if cfg.repeated_lines and prev is not None and s == prev:
            run += 1
            if run > cfg.max_repeat:
                rep_n += 1
                continue
        else:
            prev = s
            run = 1
        out.append(ln)
    t = "\n".join(out)
    if ad_n:
        stats["ads"] = ad_n
    if page_n:
        stats["page_numbers"] = page_n
    if rep_n:
        stats["repeated_lines"] = rep_n

    # 6) 半角标点 → 全角（仅相邻 CJK 时）
    if cfg.punctuation:
        t, c1 = _PUNCT_LEFT.subn(_punct_left, t)
        t, c2 = _PUNCT_RIGHT.subn(_punct_right, t)
        total = c1 + c2
        if total:
            stats["punctuation"] = total

    return CleanResult(text=t, stats=stats)
