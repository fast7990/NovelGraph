"""L1 规则层：从正文里挖人名候选，喂给 LLM 做"已知人物"提示。

**为什么不用 jieba**：P1 已经把依赖砍到 onnxruntime + tokenizers（不装 torch、
不装 FlagEmbedding），再为了人名识别引一个分词器不划算。而且这一层的产物
不直接入库，只作为 prompt 提示，精度要求低于召回——用姓氏表 + 句式模式
就够把"林三/三哥/玄阳真人"这类高频名字捞出来。

三个信号源，按精度从高到低：

1. **对话引导**：`沈鹤道："……"` / `林薇薇冷笑道："……"` —— 中文小说里最稳定的人名位置
2. **称谓后缀**：`玄阳真人`（自称后缀）、`沈师兄`、`柳掌门` —— 能抓到没姓的道号
3. **姓氏开头的高频 2-3 字窗口**：覆盖前两条漏掉的叙述性提及

第 3 条噪声最大（"白天""何必"都会被当人名），所以加权时压得很低，
且要求频次 ≥ 5 才留下。
"""

from __future__ import annotations

import re
from collections import Counter

# 常见单姓。不追求全（百家姓 504 个里生僻姓氏在小说里几乎不出现），
# 但要覆盖当代与古代通俗小说里的实际用字。
SINGLE_SURNAMES = set(
    "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦尤许何吕施张孔曹严华金魏陶姜"
    "戚谢邹喻柏水窦章云苏潘葛奚范彭郎鲁韦昌马苗凤花方俞任袁柳鲍史唐"
    "费廉岑薛雷贺倪汤滕殷罗毕郝邬安常乐于时傅皮卞齐康伍余元卜顾孟平"
    "黄和穆萧尹姚邵湛汪祁毛禹狄米贝明臧计伏成戴谈宋茅庞熊纪舒屈项祝"
    "董梁杜阮蓝闵席季麻强贾路娄危江童颜郭梅盛林刁钟徐邱骆高夏蔡田樊"
    "胡凌霍虞万支柯管卢莫房裘缪解应宗丁宣邓郁单杭洪包诸左石崔吉龚程"
    "邢滑裴陆荣翁荀羊惠甄曲家封芮羿储靳汲邴糜松井段富巫乌焦巴弓牧隗"
    "山谷车侯全班仰秋仲伊宫宁仇栾暴甘厉戎祖武符刘景詹束龙叶幸司韶郜"
    "黎蓟薄印宿白怀蒲邰从鄂索咸籍赖卓蔺屠蒙池乔阴胥能苍双闻莘党翟谭"
    "贡劳逄姬申扶堵冉宰郦雍桑桂濮牛寿通边扈燕冀浦尚农温别庄晏柴瞿阎"
    "充慕连茹习宦艾鱼容向古易慎戈廖庾终暨居衡步都耿满弘匡国文寇广禄"
    "阙东欧殳沃利蔚越夔隆师巩聂晁勾敖融冷訾辛阚那简饶空曾沙养鞠须丰"
    "巢关蒯相查后荆红游竺权逯盖益桓公"
)

COMPOUND_SURNAMES = (
    "欧阳", "太史", "端木", "上官", "司马", "东方", "独孤", "南宫", "万俟", "闻人",
    "夏侯", "诸葛", "尉迟", "公羊", "赫连", "澹台", "皇甫", "宗政", "濮阳", "公冶",
    "太叔", "申屠", "公孙", "慕容", "仲孙", "钟离", "长孙", "宇文", "司徒", "鲜于",
    "司空", "闾丘", "子车", "司寇", "巫马", "公西", "颛孙", "壤驷", "公良", "漆雕",
    "乐正", "宰父", "谷梁", "拓跋", "夹谷", "轩辕", "令狐", "段干", "百里", "呼延",
    "东郭", "南门", "羊舌", "微生", "梁丘", "左丘", "西门", "第五", "南荣", "即墨",
)

# 道号/称谓后缀：`玄阳真人`、`青云子`、`白骨夫人`。这类名字没有姓，
# 只能靠后缀识别——修真/武侠小说里这类角色往往还很重要。
TAOIST_SUFFIXES = (
    "真人", "道人", "道长", "上人", "尊者", "真君", "元君", "仙子", "仙姑",
    "夫人", "娘娘", "姥姥", "老祖", "老魔", "魔君", "剑仙", "剑圣", "刀圣",
)

# 对话引导：`XX道："` / `XX说：` / `XX冷冷地看着他，道：`
# 前面那个名字最多 4 字（复姓 + 双名），且不能是标点/引号。
_DIALOGUE = re.compile(
    r"([\u4e00-\u9fa5]{2,4})"
    r"(?:淡淡地|冷冷地|沉声|低声|轻声|笑着|苦笑|冷笑|冷哼)?"
    r"(?:说道|问道|答道|笑道|道|说|问|答)"
    r"\s*[：:]\s*[“\"]"
)

# 称谓呼语：`沈师兄` / `柳掌门`（称呼别人，不是自称）
_ADDRESS = re.compile(
    r"([\u4e00-\u9fa5]{1,3})"
    r"(?:师父|师尊|师兄|师姐|师弟|师妹|掌门|宗主|长老|前辈|大人|公子|姑娘|殿下|将军|先生|夫人)"
)

# 高频词黑名单：这些 2-3 字组合会反复出现在任意小说里，但都不是人名。
# 不追求穷尽，只压掉最常见的误报（"一声""两人""此刻"这类）。
STOPWORDS = frozenset(
    """
    一声 一时 一切 一起 一样 一直 一点 一定 一般 一起 一旦 一片 一条 一眼
    两人 三人 众人 几人 一人 旁人 有人 无人 此人 那人 这人 何人
    此刻 此时 此地 此间 当地 当时 今日 明日 昨日 次日 那天 这天
    什么 怎么 为何 如何 若是 如果 但是 可是 只是 于是 因此 所以 虽然
    已经 曾经 正在 立刻 马上 忽然 突然 顿时 缓缓 慢慢 渐渐 依旧 仍然
    声音 目光 眼神 脸色 脸色 神色 心中 心头 心里 身上 手中 面前 身后
    知道 不知 觉得 感觉 认为 发现 看见 听到 想到 记得 忘记
    说道 问道 答道 笑道 冷笑 冷哼 点头 摇头 转身 抬头 低头 回头
    大道 天道 大道 力道 味道 一道 通道 街道 知道 道理
    天地 天下 天上 地下 地上 外面 里面 前面 后面 左右 东西 南北
    青云 白衣 黑衣 红光 金光 白光 血色 血水 火光 剑气 刀光 剑光
    第一 第二 第三 最后 唯一 一切
    """.split()
)

# 单独出现时几乎不可能是"姓+名"的词（姓氏字本身的常用词）
_FALSE_PREFIX = frozenset(
    "老 小 大 这 那 哪 谁 什 怎 为 因 所 可 但 却 就 才 又 还 也 都 很 太 更 最".split()
)


def _strip_punct(word: str) -> str:
    return word.strip("　 \t\r\n，。！？、；：“”‘’（）()【】…—·")


def extract_candidates(text: str, *, top_n: int = 80, min_hits: int = 5) -> list[str]:
    """从一章（或整本书）正文里挖候选名。

    返回按分数降序的名单。调用方通常取前 40-80 个塞进 prompt——
    给太多反而会让模型倾向于把提示里的人名硬套进正文。
    """
    if not text:
        return []

    score: Counter[str] = Counter()

    # 信号 1：对话引导（权重最高，几乎不误报）
    for m in _DIALOGUE.finditer(text):
        name = _strip_punct(m.group(1))
        if _plausible(name):
            score[name] += 6

    # 信号 2：称谓呼语
    for m in _ADDRESS.finditer(text):
        name = _strip_punct(m.group(1))
        if _plausible(name):
            score[name] += 3

    # 信号 3：道号后缀
    for suffix in TAOIST_SUFFIXES:
        for m in re.finditer(rf"([\u4e00-\u9fa5]{{1,3}}{suffix})", text):
            name = _strip_punct(m.group(1))
            if _plausible(name):
                score[name] += 4

    # 信号 4：姓氏开头的高频 2-3 字窗口。
    # 这一条召回高但精度低，只在前面三条没覆盖时起作用，所以权重压到 1。
    window: Counter[str] = Counter()
    for i in range(len(text) - 1):
        ch = text[i]
        if ch not in SINGLE_SURNAMES and not any(text.startswith(c, i) for c in COMPOUND_SURNAMES):
            continue
        for size in (2, 3):
            cand = text[i : i + size]
            if len(cand) < size or not _all_cjk(cand):
                continue
            if _plausible(cand):
                window[cand] += 1
    for name, n in window.items():
        if n >= min_hits:
            score[name] += 1

    return [name for name, _ in score.most_common(top_n)]


def _all_cjk(s: str) -> bool:
    return all("\u4e00" <= c <= "\u9fa5" for c in s)


def _plausible(name: str) -> bool:
    """过滤明显不是人名的串。"""
    if not (2 <= len(name) <= 4) or not _all_cjk(name):
        return False
    if name in STOPWORDS:
        return False
    if name[0] in _FALSE_PREFIX:
        return False
    # "吃饭""走了"这种动词短语：末字是高频虚词/动词时排除
    if name[-1] in "的了着过是在有和与就都很也吗呢吧啊哦么":
        return False
    # 叠字名（"薇薇""芳芳"）保留，但"一一""二二"这类序数叠字排除
    if len(name) == 3 and name[0] == name[1] == name[2]:
        return False
    return True
