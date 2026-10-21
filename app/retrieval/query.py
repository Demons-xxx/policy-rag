"""查询规范化、结构化解析与同义扩展。

约定见 docs/架构与接口.md 3.10。这里只做检索侧的轻量处理，不依赖模型与网络，
所有函数都是纯函数，方便单测。
"""
from __future__ import annotations

import re
import unicodedata

from app.retrieval.lexical import words

# ---------------------------------------------------------------------------
# 规范化
# ---------------------------------------------------------------------------

_ZERO_WIDTH = ('\u200b', '\u200c', '\u200d', '\ufeff')
_WS_RE = re.compile(r'\s+')


def normalize(text: str) -> str:
    """NFKC 归一化：全角转半角、合并多余空白、去掉零宽字符。

    注意：NFKC 不会把「〔〕」改成半角，因此文号匹配时另行统一括号。
    """
    if text is None:
        return ''
    value = unicodedata.normalize('NFKC', str(text))
    for ch in _ZERO_WIDTH:
        value = value.replace(ch, '')
    return _WS_RE.sub(' ', value).strip()


# ---------------------------------------------------------------------------
# 中文数字与条款/章节解析
# ---------------------------------------------------------------------------

_CN_DIGITS = {
    '零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4,
    '五': 5, '六': 6, '七': 7, '八': 8, '九': 9,
}
_CN_UNITS = {'十': 10, '百': 100, '千': 1000}
_NUM = r'[0-9零〇一二两三四五六七八九十百千]+'

_ARTICLE_RE = re.compile(rf'第\s*({_NUM})\s*条')
_CHAPTER_RE = re.compile(rf'第\s*({_NUM})\s*(章|节)')
# 文号：校发〔2024〕12号 / 校发[2024]12号 / 校发（2024）12号
_DOC_NO_RE = re.compile(
    rf'([\u4e00-\u9fff]{{1,12}}?)\s*[〔\[【（(]\s*(\d{{4}})\s*[〕\]】）)]\s*(\d{{1,4}})\s*号'
)
_DOC_NO_PREFIX_TRIM = re.compile(r'^(?:根据|按照|依据|依照|参照|关于|印发|参见|见)+')

_BRACKET_MAP = str.maketrans({
    '[': '〔', '【': '〔', '(': '〔', '（': '〔',
    ']': '〕', '】': '〕', ')': '〕', '）': '〕',
})


def chinese_to_int(text: str) -> int | None:
    """中文数字转阿拉伯数字；支持「十二」「二十」「一百零三」等写法。"""
    value = normalize(text)
    if not value:
        return None
    if value.isdigit():
        return int(value)
    total = 0
    section = 0
    number = 0
    for ch in value:
        if ch in _CN_DIGITS:
            number = _CN_DIGITS[ch]
        elif ch in _CN_UNITS:
            unit = _CN_UNITS[ch]
            if number == 0:
                number = 1
            section += number * unit
            number = 0
        else:
            return None
    result = total + section + number
    return result if result > 0 else None


def doc_no_key(value: str) -> str:
    """文号比较用的规范化键：统一括号、去掉空白。"""
    text = normalize(value).translate(_BRACKET_MAP)
    return _WS_RE.sub('', text)


def article_key(value: str) -> str:
    """条款比较用的规范化键：去空白、统一「第X条」写法。"""
    text = normalize(value).replace(' ', '')
    return text


def parse_article(text: str) -> tuple[str | None, int | None]:
    """从任意文本中抓取第一个条款号，返回（条款原样写法，阿拉伯编号）。"""
    match = _ARTICLE_RE.search(normalize(text))
    if not match:
        return None, None
    raw = match.group(1)
    number = chinese_to_int(raw)
    if raw.isdigit():
        return f'第{int(raw)}条', number
    return f'第{raw}条', number


def parse_chapter_section(text: str) -> tuple[str | None, str | None]:
    """抓取第一个「第X章」与「第X节」。"""
    chapter = None
    section = None
    for match in _CHAPTER_RE.finditer(normalize(text)):
        label = f'第{match.group(1)}{match.group(2)}'
        if match.group(2) == '章' and chapter is None:
            chapter = label
        elif match.group(2) == '节' and section is None:
            section = label
    return chapter, section


def parse_doc_no(text: str) -> str | None:
    """抓取文号并统一成「校发〔2024〕12号」写法。"""
    match = _DOC_NO_RE.search(normalize(text))
    if not match:
        return None
    prefix = _DOC_NO_PREFIX_TRIM.sub('', match.group(1)) or match.group(1)
    return f'{prefix}〔{match.group(2)}〕{match.group(3)}号'


# ---------------------------------------------------------------------------
# 归口部门
# ---------------------------------------------------------------------------

# 常见高校归口部门，命中时优先返回；可在此继续补充。
KNOWN_DEPARTMENTS = (
    '实验室与设备管理处', '科学技术发展研究院', '研究生院', '教务处', '学生工作处',
    '财务处', '人事处', '离退休工作处', '国际合作与交流处', '后勤保障处',
    '保卫处', '招生办公室', '图书馆', '网络与信息中心', '校医院', '档案馆',
    '发展规划处', '审计处', '纪检监察处', '工会', '团委',
)

# 兜底：只认「处 / 办公室 / 委员会 / 中心 / 学院 / 研究院」结尾，避免把
# 「实验室」「部门」误判成部门名。
_DEPT_FALLBACK_RE = re.compile(
    r'([\u4e00-\u9fff]{2,12}?(?:处|办公室|委员会|中心|学院|研究院))'
)
_DEPT_PREFIX_TRIM = re.compile(
    r'^(?:请问|想问|想了解|了解一下|关于|有关|学校|我校|我们|贵校|本院|本处|由|归)+'
)


def find_department(text: str) -> str | None:
    """抽取问题中的归口部门词，抽不到返回 None。"""
    value = normalize(text)
    if not value:
        return None
    for name in sorted(KNOWN_DEPARTMENTS, key=len, reverse=True):
        if name in value:
            return name
    for match in _DEPT_FALLBACK_RE.finditer(value):
        candidate = _DEPT_PREFIX_TRIM.sub('', match.group(1))
        if len(candidate) >= 2:
            return candidate
    return None


# ---------------------------------------------------------------------------
# 追问判断
# ---------------------------------------------------------------------------

_FOLLOWUP_RE = re.compile(
    r'^(?:那|那么|这个|那个|该|其|它|上述|前述|同上|接着|然后|继续|还有|另外|再问|又是)'
)


def _is_followup(text: str, article: str | None, doc_no: str | None) -> bool:
    """判断问题是否依赖上一轮（省略主语/继续追问）。"""
    if not text:
        return False
    if _FOLLOWUP_RE.search(text):
        return True
    if text.endswith('呢') and len(text) <= 20:
        return True
    # 「它的适用范围」「该办法」这类指代
    if re.search(r'^(?:它|其|该|此|这)', text):
        return True
    return False


# ---------------------------------------------------------------------------
# 同义 / 简称扩展（可配置）
# ---------------------------------------------------------------------------

SYNONYMS: dict[str, list[str]] = {
    '报销': ['费用报销', '经费', '差旅费', '财务'],
    '请假': ['休假', '考勤', '销假', '假期'],
    '实验室': ['实验场所', '实验室安全', '危化品'],
    '差旅': ['出差', '差旅费', '交通费', '住宿'],
    '论文': ['学位论文', '毕业设计', '答辩'],
    '采购': ['购置', '招标', '竞价', '供应商'],
    '评奖': ['奖学金', '评优', '评审'],
    '盖章': ['用印', '公章', '印章'],
    '处分': ['违纪', '处理', '处罚'],
    '延期': ['延长', '缓交', '暂缓'],
}


def expand_terms(tokens: list[str]) -> list[str]:
    """对查询词做少量制度同义扩展，保留原词并去重。"""
    result: list[str] = []
    seen: set[str] = set()

    def add(term: str) -> None:
        term = normalize(term)
        if not term or term in seen:
            return
        seen.add(term)
        result.append(term)

    for token in tokens or []:
        token = normalize(str(token))
        if not token:
            continue
        add(token)
        for key, extras in SYNONYMS.items():
            if token == key:
                matched = True
            elif len(key) >= 2 and len(token) >= 2:
                # 允许「费用报销」命中「报销」这类包含关系
                matched = key in token or token in key
            else:
                matched = False
            if matched:
                for extra in extras:
                    add(extra)
    return result


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def parse_query(question: str) -> dict:
    """解析问题，返回契约 3.10 规定的结构（额外带 chapter/section）。"""
    text = normalize(question)
    article, article_no = parse_article(text)
    chapter, section = parse_chapter_section(text)
    doc_no = parse_doc_no(text)
    department = find_department(text)
    return {
        'text': text,
        'keywords': words(text),
        'doc_no': doc_no,
        'article': article,
        'article_no': article_no,
        'chapter': chapter,
        'section': section,
        'department': department,
        'is_followup': _is_followup(text, article, doc_no),
    }
