"""中文分词与倒排 BM25。

约定见 docs/架构与接口.md 3.8：
- ``words(text)`` 中文走 jieba，英文/数字走正则；
- ``PostingBM25`` 自己维护 posting list，不构造「词 × 文档」整矩阵，
  语料几万条时内存和查询开销都可控；``rank_bm25`` 的实现仅作公式参考。
"""
from __future__ import annotations

import logging
import math
import re
import unicodedata
from collections import Counter, defaultdict

import jieba
import numpy as np

# jieba 首次加载词典会打日志，避免污染服务输出
jieba.setLogLevel(logging.ERROR)

# 只切出「连续中文」与「英文/数字」两类片段，标点直接丢弃
_TOKEN_RE = re.compile(r'[\u4e00-\u9fff]+|[A-Za-z0-9]+')
_WS_RE = re.compile(r'\s+')

# 高频虚词/问法词，对检索没有区分度
STOPWORDS = frozenset({
    '的', '了', '是', '和', '与', '及', '或', '在', '有', '对', '为', '中', '上', '下',
    '等', '及其', '以及', '并且', '但是', '因为', '所以', '如果', '则', '而', '被',
    '哪些', '什么', '怎么', '怎样', '如何', '为什么', '是否', '能否', '可以', '需要',
    '请问', '请', '吗', '呢', '啊', '吧', '我', '我们', '你', '您', '学校', '本校',
    '规定', '要求', '内容', '情况', '相关', '进行', '应当', '应该', '必须', '关于',
    '这个', '那个', '这些', '那些', '一', '个', '条', '款', '项',
    '那', '那么', '这', '该', '其', '它', '上述', '前述', '同样', '一样',
    '第', '讲', '说', '写', '提到', '多少', '多久', '几天', '几个',
})

_EN_STOPWORDS = frozenset({
    'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'is', 'are',
    'be', 'with', 'by', 'as', 'at', 'it', 'this', 'that', 'what', 'how',
})


def _prepare(text: str) -> str:
    """NFKC + 去多余空白（不引入 query.normalize，避免循环依赖）。"""
    value = unicodedata.normalize('NFKC', str(text or ''))
    return _WS_RE.sub(' ', value).strip()


def words(text: str) -> list[str]:
    """返回检索用词序列：中文 jieba 切分，英文数字正则抽取并转小写。"""
    value = _prepare(text)
    if not value:
        return []
    result: list[str] = []
    for match in _TOKEN_RE.finditer(value):
        chunk = match.group(0)
        if chunk[0].isascii():
            token = chunk.lower()
            if token and token not in _EN_STOPWORDS:
                result.append(token)
            continue
        for piece in jieba.cut(chunk):
            piece = piece.strip()
            if not piece or piece in STOPWORDS:
                continue
            if not re.search(r'[\u4e00-\u9fffA-Za-z0-9]', piece):
                continue
            result.append(piece)
    return result


class PostingBM25:
    """倒排索引版 BM25（Okapi，k1=1.5、b=0.75）。"""

    def __init__(self, ids: list[str], token_lists: list[list[str]],
                 k1: float = 1.5, b: float = 0.75) -> None:
        self.ids = [str(item) for item in ids]
        self.k1 = float(k1)
        self.b = float(b)
        self._postings: dict[str, list[tuple[int, int]]] = defaultdict(list)
        self._idf: dict[str, float] = {}
        self._doc_len = np.zeros(len(self.ids), dtype=np.float32)

        doc_freq: Counter[str] = Counter()
        for index, tokens in enumerate(token_lists):
            counts = Counter(tokens or [])
            self._doc_len[index] = float(sum(counts.values()))
            for term, tf in counts.items():
                self._postings[term].append((index, int(tf)))
                doc_freq[term] += 1

        total = len(self.ids)
        self._avgdl = float(self._doc_len.mean()) if total else 0.0
        # BM25 常用平滑：log(1 + (N - df + 0.5) / (df + 0.5))
        self._idf = {
            term: math.log(1.0 + (total - df + 0.5) / (df + 0.5))
            for term, df in doc_freq.items()
        }

    def __len__(self) -> int:
        return len(self.ids)

    def get_scores(self, tokens: list[str]) -> np.ndarray:
        """查询词逐个累加打分；未在语料出现的词直接忽略。"""
        scores = np.zeros(len(self.ids), dtype=np.float32)
        if not self.ids or not tokens:
            return scores
        avgdl = self._avgdl or 1.0
        query_counts = Counter(tokens)
        for term, qf in query_counts.items():
            posting = self._postings.get(term)
            if not posting:
                continue
            idf = self._idf.get(term, 0.0)
            if idf <= 0.0:
                continue
            for doc_index, tf in posting:
                doc_len = float(self._doc_len[doc_index])
                denominator = tf + self.k1 * (1.0 - self.b + self.b * doc_len / avgdl)
                if denominator <= 0.0:
                    continue
                scores[doc_index] += idf * float(qf) * tf * (self.k1 + 1.0) / denominator
        return scores


# 兼容按契约以模块级名字导入
__all__ = ['words', 'PostingBM25', 'STOPWORDS']
