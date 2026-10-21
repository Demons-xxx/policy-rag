"""文本归一化等通用工具（不依赖任何业务模块）。"""
from __future__ import annotations

import re
import unicodedata

_ZERO_WIDTH = ('\u200b', '\u200c', '\u200d', '\ufeff')
_WS_RE = re.compile(r'\s+')


def normalize(text: str | None) -> str:
    """NFKC 归一化：全角转半角、合并多余空白、去掉零宽字符。

    注意：NFKC 不会把「〔〕」改成半角，因此文号匹配时另行统一括号。
    """
    if text is None:
        return ''
    value = unicodedata.normalize('NFKC', str(text))
    for ch in _ZERO_WIDTH:
        value = value.replace(ch, '')
    return _WS_RE.sub(' ', value).strip()


def squash(text: str | None) -> str:
    """去掉全部空白，用于标题/条款号的宽松比较。"""
    return normalize(text).replace(' ', '')


def same_text(left: str | None, right: str | None, strip_chars: str = '?？。.!！,，;；:：') -> bool:
    """忽略标点和空白后比较两段文字是否相同。"""
    return normalize(left).strip(strip_chars) == normalize(right).strip(strip_chars)
