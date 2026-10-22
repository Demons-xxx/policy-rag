"""回答后置校验：引用编号合法性与「拒答 / 引用」矛盾修正。

模型偶尔会：
1. 引用不存在的证据编号（如只给了 5 条证据却写 ``[证据7]``）；
2. 开头写「未在现行有效制度中找到依据」，正文却给出有出处的分析，或
   拒答句后仍带 ``[证据n]``。

本模块在流式生成结束后对完整回答做一次确定性修正（不改事实、不补内容）：
- 删除越界/悬空的引用标记；
- 拒答句与引用并存时：若拒答句之后还有成段的有依据内容，删掉拒答句；
  否则删掉引用（保留拒答），避免出现「拒答却带引用」的自相矛盾。

修正结果通过 ``revision`` 事件下发，前端用它替换已流式显示的回答。
"""
from __future__ import annotations

import re

CITE_RE = re.compile(r'\[证据(\d+)\]')
REFUSAL_RE = re.compile(
    r'未在[^。\n]{0,12}找到依据'
    r'|未找到(?:相关)?(?:依据|规定)'
    r'|没有(?:相关)?(?:依据|规定|制度)'
    r'|无法(?:回答|确定|从现有)'
    r'|未收录'
    r'|不在(?:现行)?(?:制度|资料|知识库)(?:范围|中)'
)
# 拒答句之后至少这么多字的有依据内容，才认为「拒答句是误写」。
MIN_GROUNDED_CHARS = 40
# 句末标点：遇到它们即认为拒答句结束（\\n 由字符比较处理）。
SENTENCE_ENDS = '。！？!?\n'


def _sentence_end(text: str, start: int) -> int:
    """从 start 起找第一个句末标点/换行，返回其后的位置。"""
    for index in range(start, len(text)):
        if text[index] in SENTENCE_ENDS:
            return index + 1
    return len(text)


def _tidy(text: str) -> str:
    text = re.sub(r'[ \t]+([。，；：！？、])', r'\1', text)
    text = re.sub(r'\n{3,}', r'\n\n', text)
    return text.strip()


def sanitize_answer(text: str, evidence_count: int) -> tuple[str, dict]:
    """返回（修正后的回答, 变更说明）；无变更时说明为空字典。"""
    original = str(text or '')
    if not original.strip():
        return original, {}
    upper = max(0, int(evidence_count or 0))
    removed_invalid: list[int] = []

    def drop_invalid(match: re.Match) -> str:
        number = int(match.group(1))
        if 1 <= number <= upper:
            return match.group(0)
        removed_invalid.append(number)
        return ''

    cleaned = CITE_RE.sub(drop_invalid, original)
    refusal = REFUSAL_RE.search(cleaned)
    dropped_refusal = False
    removed_for_refusal = 0
    if refusal and CITE_RE.search(cleaned):
        end = _sentence_end(cleaned, refusal.end())
        rest = _tidy(cleaned[:refusal.start()] + cleaned[end:])
        grounded = len(re.sub(r'\s+', '', rest))
        if grounded >= MIN_GROUNDED_CHARS and CITE_RE.search(rest):
            cleaned = rest
            dropped_refusal = True
        else:
            removed_for_refusal = len(CITE_RE.findall(cleaned))
            cleaned = CITE_RE.sub('', cleaned)

    cleaned = _tidy(cleaned)
    changes: dict = {}
    if removed_invalid:
        changes['removed_invalid'] = removed_invalid
    if removed_for_refusal:
        changes['removed_for_refusal'] = removed_for_refusal
    if dropped_refusal:
        changes['dropped_refusal'] = True
    if not changes or not cleaned:
        return original, {}
    return cleaned, changes


__all__ = ['sanitize_answer', 'CITE_RE', 'REFUSAL_RE', 'SENTENCE_ENDS']
