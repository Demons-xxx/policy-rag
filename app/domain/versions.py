"""制度时效与版本治理（领域包 policy 的规则，集中一处便于替换）。

检索层、入库层、API 层都通过这里判断「某份文件在某一天是否有效」、
「同主题是否存在更新的版本」，不再各自写字符串比较。

状态词表（可被领域包覆盖）：
    active       现行有效
    superseded   已被新版替代
    repealed     已废止（不再检索）
    draft        草稿（未发布）
    expired      到期失效
"""
from __future__ import annotations

import re

from app.core.text import normalize, squash

STATUS_ACTIVE = 'active'
STATUS_SUPERSEDED = 'superseded'
STATUS_REPEALED = 'repealed'
STATUS_DRAFT = 'draft'
STATUS_EXPIRED = 'expired'

# 检索时直接排除的状态
HIDDEN_STATUS = {STATUS_REPEALED, STATUS_DRAFT}

_STATUS_ALIASES = {
    '现行': STATUS_ACTIVE, '有效': STATUS_ACTIVE, 'active': STATUS_ACTIVE,
    '被替代': STATUS_SUPERSEDED, '已废止': STATUS_REPEALED, '废止': STATUS_REPEALED,
    'repealed': STATUS_REPEALED, 'draft': STATUS_DRAFT, 'expired': STATUS_EXPIRED,
}

# 标题尾部版本标记，如「（2025年修订）」「（2023年版）」；把新旧版本归到同一主题
VERSION_SUFFIX_RE = re.compile(r'[（(][^（）()]{0,20}?(?:修订|版|年)[^（）()]{0,20}?[）)]$')


def normalize_status(value: str | None) -> str:
    text = normalize(value).lower()
    return _STATUS_ALIASES.get(text, text or STATUS_ACTIVE)


def title_key(unit: dict) -> str:
    """主题键：去掉书名号、空白和尾部版本标记，使新旧版本归为同一主题。"""
    title = squash(unit.get('doc_title') or '').replace('《', '').replace('》', '')
    stripped = VERSION_SUFFIX_RE.sub('', title)
    return stripped or title


def effective_from_ok(unit: dict, as_of: str) -> bool:
    """生效日期晚于基准日的单元不参与检索（尚未施行）。"""
    start = str(unit.get('effective_from') or '')
    return not start or start <= as_of


def in_window(unit: dict, as_of: str) -> bool:
    start = str(unit.get('effective_from') or '')
    end = str(unit.get('effective_to') or '')
    if start and start > as_of:
        return False
    if end and end < as_of:
        return False
    return True


def expired(unit: dict, as_of: str) -> bool:
    """effective_to 早于基准日：该版本已失效。"""
    end = str(unit.get('effective_to') or '')
    return bool(end) and end < as_of


def current_documents(units: list[dict], as_of: str) -> dict[str, str]:
    """同主题下选出 as_of 时点的现行文档：优先 active 且在有效期内，再比生效日期。

    返回 {主题键: doc_id}。
    """
    by_title: dict[str, dict[str, dict]] = {}
    for unit in units:
        if normalize_status(unit.get('status')) in HIDDEN_STATUS:
            continue
        key = title_key(unit)
        doc_id = str(unit.get('doc_id') or '')
        if not key or not doc_id:
            continue
        by_title.setdefault(key, {}).setdefault(doc_id, unit)

    current: dict[str, str] = {}
    for key, docs in by_title.items():
        ranked: list[tuple[int, str, str]] = []
        for doc_id, unit in docs.items():
            status = normalize_status(unit.get('status'))
            start = str(unit.get('effective_from') or '')
            if start and start > as_of:
                continue
            active_now = 1 if (status == STATUS_ACTIVE and in_window(unit, as_of)) else 0
            ranked.append((active_now, start, doc_id))
        if not ranked:
            # 全部已过期时仍指认最新生效版本，避免把旧版误判成「被替代」
            for doc_id, unit in docs.items():
                ranked.append((0, str(unit.get('effective_from') or ''), doc_id))
        ranked.sort()
        current[key] = ranked[-1][2]
    return current


def is_stale(unit: dict, current_docs: dict[str, str], as_of: str) -> bool:
    """该单元是否属于「已被替代/已过期」的旧版本。"""
    current_doc = current_docs.get(title_key(unit))
    doc_id = str(unit.get('doc_id') or '')
    return bool((current_doc and doc_id != current_doc) or expired(unit, as_of))


def version_note(unit: dict, current_docs: dict[str, str], doc_samples: dict[str, dict] | None = None) -> str | None:
    """生成给用户看的版本提示；没有更新版本时按失效日期给通用提示。"""
    key = title_key(unit)
    current_doc = current_docs.get(key)
    title = str(unit.get('doc_title') or '').strip()
    label = str(unit.get('version_label') or '').strip()
    version = label or '旧版'
    if not current_doc or current_doc == str(unit.get('doc_id')):
        end = str(unit.get('effective_to') or '').strip()
        if end:
            return f'《{title}》{version}已于{end}废止，内容仅作参考'
        return None
    sample = (doc_samples or {}).get(current_doc) or {}
    current_label = str(sample.get('version_label') or '').strip()
    effective = str(sample.get('effective_from') or '').strip()
    head = f'《{title}》{current_label}' if current_label else f'《{title}》现行版本'
    if effective:
        return f'{head}自{effective}起施行，旧版内容仅作参考'
    return f'{head}已替代旧版，旧版内容仅作参考'
