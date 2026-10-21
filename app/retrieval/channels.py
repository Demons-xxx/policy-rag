"""候选召回通道：BGE 向量、BM25、文号/条款号/章节/标题精确命中。

每个通道都是「输入 -> [(passage_id, score)] 或 {通道: [unit_id]}」的纯函数，
不持有检索器状态，便于单测与替换。编排（何时调用、异常如何降级）留在
``app.retrieval.engine``。
"""
from __future__ import annotations

import re

import numpy as np

from app.retrieval.lexical import PostingBM25, words
from app.retrieval.query import article_key, chinese_to_int, doc_no_key, expand_terms, normalize

# 各路召回条数
DENSE_TOP = 50
KEYWORD_TOP = 50


# ---------------------------------------------------------------------------
# 稠密 / 稀疏通道
# ---------------------------------------------------------------------------

def where_filter(doc_filter: str | None) -> dict | None:
    """向量后端的元数据过滤条件：只保留指定文档的片段。"""
    if not doc_filter:
        return None
    return {'doc_id': {'$in': [doc_filter]}}


def dense_candidates(encoder, backend, rewritten: str,
                     doc_filter: str | None = None,
                     top_k: int = DENSE_TOP) -> tuple[list[tuple[str, float]], str | None]:
    """BGE 向量召回；返回 ``(hits, note)``，失败时 hits 为空且 note 为降级说明。"""
    try:
        matrix = encoder.encode([rewritten], query=True)
        if matrix is not None and len(matrix):
            return backend.search(
                np.asarray(matrix, dtype=np.float32)[0],
                top_k,
                where=where_filter(doc_filter),
            ), None
        return [], None
    except Exception as exc:
        return [], f'向量检索不可用（{type(exc).__name__}: {exc}），已回退关键词检索'


def keyword_candidates(bm25: PostingBM25, passage_ids: list[str], rewritten: str,
                       top_k: int = KEYWORD_TOP) -> tuple[list[tuple[str, float]], str | None]:
    """BM25 召回；返回 ``(hits, note)``，理论不可达的异常同样给出 note。"""
    hits: list[tuple[str, float]] = []
    tokens = expand_terms(words(rewritten))
    try:
        scores = bm25.get_scores(tokens)
        if len(scores):
            limit = min(top_k, len(scores))
            order = np.argsort(-scores, kind='stable')[:limit]
            for index in order:
                value = float(scores[int(index)])
                if value <= 0.0:
                    continue
                hits.append((passage_ids[int(index)], value))
    except Exception as exc:  # pragma: no cover - 理论不可达
        return [], f'关键词检索失败（{type(exc).__name__}: {exc}）'
    return hits, None


# ---------------------------------------------------------------------------
# 精确 / 半精确通道
# ---------------------------------------------------------------------------

def _label_parts(label: str) -> tuple[str, int | None] | None:
    """解析「第三章 / 第3节」→ (类别, 序号)。"""
    match = re.search(r'第\s*([0-9零〇一二两三四五六七八九十百千]+)\s*([章节])', label or '')
    if not match:
        return None
    return match.group(2), chinese_to_int(match.group(1))


def _same_label(left: str, right: str) -> bool:
    """章节标题比较：原文包含或「章/节 + 序号」相同都算命中。"""
    if not left or not right:
        return False
    if right in left:
        return True
    left_parts = _label_parts(left)
    right_parts = _label_parts(right)
    return bool(left_parts and right_parts and left_parts == right_parts)


def _order_of(unit: dict) -> int:
    try:
        return int(unit.get('order') or 0)
    except (TypeError, ValueError):
        return 0


def exact_channels(units: list[dict], unit_map: dict[str, dict], parsed: dict) -> dict[str, list[str]]:
    """条款号 / 文号 / 章节 / 标题 四路精确或半精确召回。"""
    channels: dict[str, list[str]] = {}
    doc_no = parsed.get('doc_no')
    article_no = parsed.get('article_no')
    article_text = parsed.get('article')
    chapter = parsed.get('chapter')
    section = parsed.get('section')

    doc_key = doc_no_key(doc_no) if doc_no else None

    def unit_doc_key(unit: dict) -> str:
        return doc_no_key(unit.get('doc_no') or '')

    # 文号：该文号下所有单元按原文顺序
    if doc_key:
        matched = [
            str(unit['id']) for unit in units
            if unit.get('id') and unit_doc_key(unit) == doc_key
        ]
        matched.sort(key=lambda uid: _order_of(unit_map[uid]))
        if matched:
            channels['文号'] = matched[:200]

    # 条款号：优先 article_no，其次条款文本
    if article_no is not None or article_text:
        matched = []
        for unit in units:
            uid = str(unit.get('id') or '')
            if not uid:
                continue
            if doc_key and unit_doc_key(unit) != doc_key:
                continue
            same = False
            unit_no = unit.get('article_no')
            if article_no is not None and unit_no is not None:
                try:
                    same = int(unit_no) == int(article_no)
                except (TypeError, ValueError):
                    same = False
            if not same and article_text:
                same = article_key(unit.get('article') or '') == article_key(article_text)
            if same:
                matched.append(uid)
        matched.sort(key=lambda uid: _order_of(unit_map[uid]))
        if matched:
            channels['条款号'] = matched[:50]

    # 章节：单元 chapter/section/heading_path 包含「第X章」「第X节」
    if chapter or section:
        matched = []
        for unit in units:
            uid = str(unit.get('id') or '')
            if not uid:
                continue
            texts = [
                normalize(str(unit.get('chapter') or '')),
                normalize(str(unit.get('section') or '')),
            ] + [normalize(str(item)) for item in (unit.get('heading_path') or [])]
            hit = False
            if chapter and any(_same_label(text, chapter) for text in texts if text):
                hit = True
            if section and any(_same_label(text, section) for text in texts if text):
                hit = True
            if hit:
                matched.append(uid)
        matched.sort(key=lambda uid: _order_of(unit_map[uid]))
        if matched:
            channels['章节'] = matched[:50]

    # 标题：问题里的长词命中文档标题（同一文档的单元按原文顺序）
    keywords = [token for token in (parsed.get('keywords') or []) if len(token) >= 2]
    if keywords:
        scored: list[tuple[int, int, str]] = []
        for unit in units:
            uid = str(unit.get('id') or '')
            if not uid:
                continue
            title = normalize(str(unit.get('doc_title') or ''))
            hits = sum(1 for token in keywords if token in title)
            if hits:
                scored.append((-hits, _order_of(unit), uid))
        scored.sort()
        if scored:
            channels['标题'] = [item[2] for item in scored][:50]
    return channels


# ---------------------------------------------------------------------------
# 通道 -> 单元的挂接（link）
# ---------------------------------------------------------------------------

def linked_passage(unit_id: str, unit_passages: dict[str, list[dict]]) -> str | None:
    """精确通道只召回 unit_id 时，挂接该单元按位置排序后的首个片段。

    候选在融合阶段仍以「单元」为单位计分，片段仅用于后续取原文窗口。
    """
    passages = unit_passages.get(unit_id) or []
    if not passages:
        return None
    return str(passages[0].get('id'))


__all__ = [
    'DENSE_TOP', 'KEYWORD_TOP',
    'dense_candidates', 'keyword_candidates', 'exact_channels',
    'where_filter', 'linked_passage',
]
