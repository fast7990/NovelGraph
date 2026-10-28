"""泛称词表与规范名打分。

《三国演义》那张图踩过的坑，值得写在这里当路标：

模型把「丞相」「将军」「主公」「天子」这类**尊称/官职**填进了 aliases，而 L3 又拿
"别名指向某人"当合并依据，于是曹操(别名 孟德/丞相)、董卓(别名 丞相)、诸葛武侯(别名
丞相) 被拧成一根链表；并查集没有长度上限，最终长出 2961 条提及的 blob，
曹操本人连别名都不剩（被 `[:12]` 截断掉），从实体表和搜索里彻底消失。

两道防线：

1. **L2 抽取层** 用 `is_honorific` 直接剔掉泛称别名（提示词里也明说禁止）；
2. **L3 对齐层** 只让 `is_mergeable_alias` 放行的专名触发合并，且限制连通分量大小。

词表只做**精确匹配**（外加「老/大/小/众」+ 泛称这种两层组合），不做包含匹配：
`玄阳真人` 是名字，`真人` 才是泛称；用包含匹配会把整个修真小说的角色名一起洗掉。
"""

from __future__ import annotations

from app.services.graph.candidates import COMPOUND_SURNAMES, SINGLE_SURNAMES

# 精确命中即判为泛称：这些词在书里远不止指向一个人，进了 aliases 就是合并事故。
# 覆盖三类：官职爵位、尊称谦称、群体称谓。现代/古典/修真小说通用。
HONORIFICS = frozenset(
    """
    丞相 相国 宰相 相爷 太师 太傅 太保 少傅 少保 太宰 少宰 尚父 司徒 司空 大司马 司马
    大将军 将军 上将军 骠骑将军 车骑将军 前将军 后将军 左将军 右将军 中郎将 校尉 都尉
    都督 大都督 提督 总兵 元帅 节度使 刺史 州牧 太守 县令 县长 令尹 令公 中书令 尚书令
    尚书 侍郎 郎中 主簿 参军 长史 别驾 从事 功曹 太尉 御史 大夫 都头 押司 教头 里正
    保长 族长 村长 掌门 宗主 长老 堂主 舵主 护法 香主 帮主 当家 掌柜 东家 员外 秀士
    举人 进士 秀才 童生 太学生 侍中 黄门 常侍 宦官 太监 宫人 太医 御医 书童 丫鬟 侍女
    主公 明公 恩相 大人 老爷 公子 少爷 小姐 姑娘 夫人 娘子 太太 先生 女士 阁下 足下
    君侯 贤弟 贤兄 兄长 哥哥 兄弟 姐姐 妹妹 师父 师傅 师叔 师伯 师兄 师姐 师弟 师妹
    前辈 后辈 晚辈 弟子 徒儿 大王 天子 陛下 殿下 千岁 万岁 圣上 皇上 皇帝 皇后 太后
    太子 公主 郡王 亲王 王爷 侯爷 太爷 老太爷 老太太 老夫人 老贼 匹夫 老匹夫 竖子
    小子 后生 小生 贫道 贫僧 老衲 贫尼 施主 居士 道长 真人 上人 尊者 老祖 老人家
    老丈 老翁 壮士 好汉 义士 英雄 诸位 各位 众位 列位 众将 众官 群臣 百官 我等 尔等
    汝等 你等 那人 此人 众人 旁人 来人 手下 部下 属下 左右 亲随 侍卫 亲兵
    朕 孤 寡人 微臣 臣 妾 妾身 奴家 在下 小可 愚兄 愚弟 为兄 为师 老夫 老朽 老身
    鄙人 不才 末将 贫道 小道 小道童 军师 先生 舍人 门客 食客
    """.split()
)

# 以这些结尾的多字词多为封号/官称（汉中王、温侯、汉献帝、董太师）。
# 它们**不是**泛称——往往唯一指向某人——但也不该拿来触发合并：
# 模型把「汉中王」塞给陈留王就是这么翻车的。降分 + 不合并，双保险。
TITLE_SUFFIXES = (
    "王", "公", "侯", "帝", "后", "君", "相", "帅", "将", "臣", "祖", "宗", "爷",
    "大人", "先生", "夫人", "公主", "将军", "丞相", "太师", "都督", "太守", "刺史",
)

# 「老将军」「众将军」「这厮」这类"前缀 + 泛称"的组合
_VAGUE_PREFIXES = ("老", "大", "小", "众", "诸", "列", "这", "那", "此", "小老")

# 表字的高频收尾字。中文里「字」多为 2 字且以这类字收尾（孟德/孔明/云长/仲谋），
# 而本名的尾字杂乱得多。这是唯一能在**结构上**区分「关羽」和「关云长」的信号。
# 刻意收窄：忠、良、贤、信、仁、文、武、元、世、方、正、平、安 这些常见真名尾字一律不放，
# 否则黄忠、张辽这类本名会被误压。
COURTESY_TAILS = frozenset("德明长龙达瑾符谋远敬言升先仲叔季甫卿之玄素孝义礼智俭恭让温建若")

# 合并用的别名长度窗口。1 字（"飞""云"）几乎全是噪声；
# 5 字以上基本是模型概括出来的短句（"桃园三结义"），不是称谓。
_ALIAS_MIN_LEN = 2
_ALIAS_MAX_LEN = 4


def is_honorific(name: str) -> bool:
    """是不是泛称/尊称/官职。精确匹配 + 「前缀+泛称」两层组合。"""
    s = (name or "").strip()
    if not s:
        return True
    if s in HONORIFICS:
        return True
    for p in _VAGUE_PREFIXES:
        if s.startswith(p) and s[len(p) :] in HONORIFICS:
            return True
    return False


def is_mergeable_alias(alias: str, name: str = "") -> bool:
    """这个别名够不够"专"，能拿来当两个实体是同一个人 的证据。

    宁可少并：漏并的代价是子图里一个人拆成两个节点（看得见、能人工合），
    错并的代价是两个人焊成一个（曹操消失、关系全糊），后者不可逆得多。
    """
    a = (alias or "").strip()
    if not a or a == name:
        return False
    if not (_ALIAS_MIN_LEN <= len(a) <= _ALIAS_MAX_LEN):
        return False
    if is_honorific(a):
        return False
    if a.endswith(TITLE_SUFFIXES):
        return False
    return True


def _surname_prefix(name: str) -> tuple[bool, bool]:
    """(是否以姓氏开头, 是否复姓)。"""
    if len(name) >= 2 and name[:2] in COMPOUND_SURNAMES:
        return True, True
    if name[:1] in SINGLE_SURNAMES:
        return True, False
    return False, False


def canonical_score(name: str) -> int:
    """给「用哪个名字当规范名」打分，越高越像本名。

    只看字面结构，不依赖提及量——提及量本身已被泛称灌过水（`丞相`全书 547 次，
    七成算到了董卓头上），拿它当选主名的依据等于用污染源做裁判。
    实际排序时分数优先、块命中数只做 tie-break。

    结构上能拿到三个信号：
    - 2 字最像本名（曹操/刘备/关羽），3 字「姓+双名」次之（诸葛亮/司马懿）；
    - 尾字落在 `COURTESY_TAILS` 里多半是「字」（孟德/孔明/云长），往下压；
    - 以封号收尾且不带复姓的（汉中王/汉献帝）再往下压。

    标定样本（三国演义实测）：
        曹操 45 · 孟德 25 · 曹孟德 30 · 诸葛亮 50 · 诸葛武侯 40 · 孔明 25
        关羽 45 · 关云长 30 · 刘备 45 · 刘玄德 30 · 玄德 25
        陈留王 25 · 汉中王 5 · 汉献帝 5 · 丞相 -1000
    """
    s = (name or "").strip()
    if not s or is_honorific(s):
        return -1000

    n = len(s)
    surnamed, compound = _surname_prefix(s)

    if n == 2:
        score = 45
        if s[-1] in COURTESY_TAILS:
            score -= 20
    elif n == 3:
        # 「姓+双名」是规范写法（诸葛亮/司马懿），但「姓+双字字」长得一模一样
        # （刘玄德/关云长/孙仲谋），只能靠尾字表拆开
        score = 30 + (20 if surnamed else 0)
        if s[-1] in COURTESY_TAILS:
            score -= 20
    elif n == 4:
        # 复姓开头的 4 字（诸葛武侯/诸葛孔明）指向唯一、信息量足，给中位分
        score = 20 + (20 if compound else 0)
    else:
        score = 5

    if not compound and s.endswith(TITLE_SUFFIXES):
        score -= 25
    return score
