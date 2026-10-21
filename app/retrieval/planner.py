"""查询拆解（QueryPlanner）：把「A 和 B 分别怎么规定」拆成多个检索子句。

一句多问（跨制度、跨事项）时，单次检索容易被其中一个话题「吃掉」证据位。
这里用保守的规则判断是否真的多问：必须出现「分别/同时/以及/；」这类连接词，
且拆出的子句都够长，否则原样返回——单话题问题不会因为出现「和」就被拆坏。

不做模型调用：纯规则、可单测、零延迟。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.core.text import normalize

#: 出现任一标记才考虑拆解
MARKERS = ('分别', '各自', '同时', '以及', '另外', '此外', '还有')
MARKER_RE = re.compile('|'.join(MARKERS) + r'|[；;]')
#: 拆解用的分隔：标记 + 分号；顿号与「和/与」只在片段内部再拆
SPLIT_RE = re.compile('|'.join(MARKERS) + r'|[；;]')
INNER_SPLIT_RE = re.compile(r'[和与、]')
#: 句尾疑问词与语气词，清理后子句更接近「检索关键词」
TAIL_RE = re.compile(
    r'(分别是|分别|各自|分别要求|要求|规定|怎么规定|怎么样|怎么办|如何|怎么|'
    r'是什么|是多少|多少|多久|几天|多长时间|哪些|什么|是否|能否|可以|需不需要|需要|'
    r'吗|呢|的|是)([？?。！!，,\s]*)$'
)
EDGE_RE = re.compile(r'^[，,、。.；;：:？?！!\s]+|[，,、。.；;：:？?！!\s]+$')


@dataclass(frozen=True)
class QueryPlan:
    """拆解结果：``sub_queries`` 至少含原问题一条。"""

    original: str
    sub_queries: tuple[str, ...] = field(default_factory=tuple)
    reason: str = ''

    @property
    def is_multi(self) -> bool:
        return len(self.sub_queries) > 1

    def as_stage(self) -> dict:
        return {'sub_queries': list(self.sub_queries), 'reason': self.reason}


def _clean(part: str) -> str:
    """去掉句首句尾标点与句尾疑问词；剩余太长时截断到 60 字。"""
    text = EDGE_RE.sub('', normalize(part))
    previous = None
    while previous != text:
        previous = text
        text = TAIL_RE.sub('', text)
        text = EDGE_RE.sub('', text)
    return text[:60]


ASPECT_LIMIT = 8
#: 拆出来只剩这些动词/疑问词时没有检索价值，直接丢弃
STOP_PARTS = {'要遵守', '遵守', '需要', '要注意', '注意', '请问', '想问', '分别', '各自', '同时', '以及'}


def _aspect_variants(part: str) -> list[str]:
    """「共享语境 + 短事项名」的子句再生成一个聚焦变体。

    例：「用科研经费购买实验室危险化学品,采购」→ 追加「化学品 采购」。
    长句里的共享语境（科研经费、购买）会把短事项压下去，聚焦变体让事项本身
    去召回它自己的条款；太短或没有逗号的子句不生成。
    """
    text = normalize(part)
    if ',' not in text and '，' not in text:
        return []
    head, _, tail = text.replace('，', ',').rpartition(',')
    tail = _clean(tail)
    if not (2 <= len(tail) <= ASPECT_LIMIT):
        return []
    anchor = _clean(head)[-5:]
    if len(anchor) < 2:
        return []
    variant = f'{anchor} {tail}'.strip()
    return [variant] if variant != text else []


def _split_inner(part: str) -> list[str]:
    """片段内部还有「和/与/、」且两侧都像事项名时再拆一层。"""
    if len(part) < 8 or len(INNER_SPLIT_RE.findall(part)) > 3:
        return [part]
    pieces = [item for item in INNER_SPLIT_RE.split(part) if item]
    if len(pieces) < 2 or any(len(_clean(item)) < 2 for item in pieces):
        return [part]
    return pieces


def _variants(text: str, original: str) -> list[str]:
    """主检索句 + 可选的用户原话（改写差异明显时）。"""
    variants = [text]
    other = _clean(original)
    if not other or len(other) < 4:
        return variants
    if other == variants[0]:
        return variants
    if other in variants[0] or variants[0] in other:
        return variants
    # 字符重合度过高说明只是加了修饰词，不必重复检索
    overlap = len(set(other) & set(variants[0])) / max(1, len(set(other)))
    if overlap >= 0.95:
        return variants
    variants.append(other)
    return variants


def plan_query(text: str, original: str = '', titles: list[str] | tuple[str, ...] = ()) -> QueryPlan:
    """返回检索计划。

    - 明显多问（出现「分别/同时/以及/；」）→ 拆成多个子句分别检索；
    - 否则如果多轮改写后的问题与用户原话差异较大 → 两者都检索再合并，
      避免改写漂移（例如把「报销单多久交」改写成「差旅费报销时限」）漏掉原文能命中的条款。
    """
    original_text = normalize(strip_unknown_docs(text, titles))
    if not original_text:
        return QueryPlan(original=original_text, sub_queries=('',), reason='空问题')

    variants = _variants(original_text, original)
    candidate = _clean(original_text)
    if len(candidate) < 14 or not MARKER_RE.search(original_text):
        reason = '单话题，未拆解'
        if len(variants) > 1:
            reason = '改写与原问题分别检索'
        return QueryPlan(original=original_text, sub_queries=tuple(variants), reason=reason)

    parts: list[str] = []
    for raw in SPLIT_RE.split(original_text):
        cleaned = _clean(raw)
        if not cleaned:
            continue
        inner = _split_inner(cleaned)
        parts.extend(inner)
        for piece in inner:
            parts.extend(_aspect_variants(piece))

    sub_queries: list[str] = []
    for part in parts:
        cleaned = _clean(part)
        if len(cleaned) < 2 or cleaned in STOP_PARTS or cleaned in sub_queries:
            continue
        sub_queries.append(cleaned)

    if len(sub_queries) < 2:
        return QueryPlan(original=original_text, sub_queries=tuple(variants),
                         reason='拆出的子句不足两条，按单问题检索')

    # 子句总数过多时只保留前 4 条，避免一次提问把检索预算打散
    if len(sub_queries) > 4:
        sub_queries = sub_queries[:4]
    return QueryPlan(original=original_text, sub_queries=tuple(sub_queries),
                     reason=f'检测到 {len(sub_queries)} 个子问题')


#: 只匹配「像文件名」的片段：书名号，或以办法/细则/条例/制度/规范/规则结尾
DOC_NAME_RE = re.compile(
    r'《([^》]{2,30})》|([一-龥A-Za-z0-9（）()]{2,40}?(?:办法|细则|条例|制度|规范|规则))')
#: 含疑问词的片段不是文件名（避免把「…分别要遵守什么规定」当成文档删掉）
QUESTION_HINTS = ('什么', '怎么', '怎样', '如何', '是否', '哪些', '多少', '几', '吗', '呢', '分别')
#: 书名号/文号类 token 与知识库标题的最低重合度，低于它视为模型臆造的文件名
MIN_TITLE_OVERLAP = 0.6


def _overlap(left: str, right: str) -> float:
    left_set, right_set = set(left), set(right)
    return len(left_set & right_set) / max(1, len(left_set))


def strip_unknown_docs(text: str, known_titles: list[str] | tuple[str, ...] = ()) -> str:
    """去掉改写里凭空出现的文件名（模型常把不存在的《XX办法》编进检索问句）。

    只有在提供了知识库标题时才做删减；命中已知标题（字符重合度 ≥ 0.6）时保留原词。
    """
    titles = [normalize(item) for item in known_titles if normalize(item)]
    if not titles or not text:
        return text
    result = text
    for match in DOC_NAME_RE.finditer(text):
        token = normalize(match.group(1) or match.group(2) or '')
        if not token or len(token) < 4:
            continue
        if any(hint in token for hint in QUESTION_HINTS):
            continue
        if any(_overlap(token, title) >= MIN_TITLE_OVERLAP or token in title for title in titles):
            continue
        result = result.replace(match.group(0), ' ')
    return re.sub(r'\s+', ' ', result).strip()


__all__ = ['QueryPlan', 'plan_query', 'strip_unknown_docs', 'MARKERS']
