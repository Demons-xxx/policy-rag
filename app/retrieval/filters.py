"""检索过滤：状态、生效/失效、归口部门、表头行、章节导语去重与相关性门控。

时效与版本的判断全部委托 ``app.domain.versions``（领域规则只在一处），
本模块只负责把规则按检索流程的顺序应用，不做字符串日期比较。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.core.text import normalize
from app.domain import versions as versioning
from app.retrieval.fusion import EXACT_CHANNELS
from app.retrieval.query import doc_no_key

# 单条结果的最低 BM25 分：向量分不足时用它做第二道门槛
RESULT_MIN_BM25 = 8.0
# 相关性闸门拒答文案（与 docs/架构与接口.md 2.5 一致）
GATE_REFUSAL_NOTE = '未找到与问题直接相关的制度条款，建议换一种问法或补充事项名称'


# ---------------------------------------------------------------------------
# 时效/版本：只做适配，规则在 app/domain/versions.py
# ---------------------------------------------------------------------------

def unit_status(unit: dict) -> str:
    return versioning.normalize_status(unit.get('status'))


def is_repealed(unit: dict) -> bool:
    """已废止单元不参与检索（其余状态由时效与版本逻辑处理）。"""
    return unit_status(unit) == versioning.STATUS_REPEALED


def effective_from_ok(unit: dict, as_of: str) -> bool:
    return versioning.effective_from_ok(unit, as_of)


def in_window(unit: dict, as_of: str) -> bool:
    return versioning.in_window(unit, as_of)


def expired(unit: dict, as_of: str) -> bool:
    return versioning.expired(unit, as_of)


def title_key(unit: dict) -> str:
    return versioning.title_key(unit)


def current_documents(units: list[dict], as_of: str) -> dict[str, str]:
    return versioning.current_documents(units, as_of)


def is_stale(unit: dict, current_docs: dict[str, str], as_of: str) -> bool:
    return versioning.is_stale(unit, current_docs, as_of)


# ---------------------------------------------------------------------------
# 结构过滤：表头行 / 章节导语
# ---------------------------------------------------------------------------

def table_header_only(unit: dict) -> bool:
    """表头行没有数据，命中它只会让模型看到列名而看不到内容。"""
    if str(unit.get('kind') or '') != 'table_row':
        return False
    table = unit.get('table') or {}
    headers = [str(cell).strip() for cell in (table.get('headers') or [])]
    row = [str(cell).strip() for cell in (table.get('row') or [])]
    return bool(headers) and row == headers


def heading_only(unit: dict) -> bool:
    """判断单元是否只是章节导语（无实质正文），这类单元保留但不抢位。"""
    if unit.get('heading_only'):
        return True
    if str(unit.get('kind') or '') != 'section':
        return False
    text = normalize(str(unit.get('text') or ''))
    if not text:
        return True
    heading = normalize(' '.join(
        str(item) for item in (unit.get('heading_path') or []) if item
    )).replace(' ', '')
    if heading and text.replace(' ', '') in heading:
        return True
    if len(text) <= 30:
        return True
    return False


def heading_flags(usable: list[str], unit_map: dict[str, dict]) -> dict[str, bool]:
    return {unit_id: heading_only(unit_map[unit_id]) for unit_id in usable}


def dedupe_heading_units(usable: list[str], flags: dict[str, bool],
                         unit_map: dict[str, dict]) -> list[str]:
    """同一章节已有正文单元时，章节导语不重复占位。"""
    substantive_sections: set[tuple[str, str]] = set()
    for unit_id in usable:
        if flags[unit_id]:
            continue
        unit = unit_map[unit_id]
        substantive_sections.add(
            (str(unit.get('doc_id')), normalize(str(unit.get('chapter') or '')))
        )
    kept: list[str] = []
    for unit_id in usable:
        unit = unit_map[unit_id]
        section_key = (
            str(unit.get('doc_id')),
            normalize(str(unit.get('chapter') or '')),
        )
        if flags[unit_id] and section_key in substantive_sections:
            continue
        kept.append(unit_id)
    return kept


# ---------------------------------------------------------------------------
# 硬过滤
# ---------------------------------------------------------------------------

@dataclass
class FilterOutcome:
    usable: list[str] = field(default_factory=list)
    stale_ids: set[str] = field(default_factory=set)
    stale_candidates: list[tuple[str, float]] = field(default_factory=list)


def apply_hard_filters(records: dict[str, dict], unit_map: dict[str, dict],
                       current_docs: dict[str, str], as_of: str,
                       doc_filter: str | None = None,
                       department_filter: str | None = None,
                       explicit_doc_key: str | None = None) -> FilterOutcome:
    """状态 / 表头 / 文档 / 部门 / 生效日期 / 旧版本 的硬过滤。

    旧版内容默认不进证据，只记入 stale_candidates 供版本提示；用户明确
    点名旧文号时放行。返回顺序与 records 的插入顺序一致（即融合顺序）。
    """
    outcome = FilterOutcome()
    for unit_id in records:
        unit = unit_map[unit_id]
        if is_repealed(unit):
            continue
        if table_header_only(unit):
            continue
        if doc_filter and str(unit.get('doc_id')) != doc_filter:
            continue
        if department_filter:
            unit_dept = normalize(str(unit.get('department') or ''))
            if department_filter not in unit_dept:
                continue
        if not effective_from_ok(unit, as_of):
            continue
        if is_stale(unit, current_docs, as_of):
            outcome.stale_ids.add(unit_id)
            outcome.stale_candidates.append((unit_id, float(records[unit_id]['rrf'])))
            named_old_doc = bool(
                explicit_doc_key
                and doc_no_key(unit.get('doc_no') or '') == explicit_doc_key
            )
            if not named_old_doc:
                continue
        outcome.usable.append(unit_id)
    return outcome


# ---------------------------------------------------------------------------
# 相关性门控
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class GateDecision:
    blocked: bool
    min_dense: float
    max_dense: float
    has_exact: bool


def relevance_gate(dense_hits: list[tuple[str, float]],
                   exact_channels: dict[str, list[str]],
                   min_dense: float) -> GateDecision:
    """问题与知识库完全无关时，不拿低分结果硬凑证据。"""
    max_dense = max((float(score) for _, score in dense_hits), default=0.0)
    has_exact = any(exact_channels.get(name) for name in EXACT_CHANNELS)
    blocked = min_dense > 0 and not has_exact and max_dense < min_dense
    return GateDecision(blocked=blocked, min_dense=min_dense,
                        max_dense=max_dense, has_exact=has_exact)


def passes_result_gate(record: dict, min_dense: float, max_dense: float) -> bool:
    """单条结果也要过相关性：向量分太低、关键词也不强、又不是精确命中则丢弃。"""
    if max_dense > 0 and min_dense > 0:
        dense_ok = float(record['dense']) >= min_dense
        keyword_ok = float(record['bm25']) >= RESULT_MIN_BM25
        exact_ok = bool(record['channels'] & set(EXACT_CHANNELS))
        if not (dense_ok or keyword_ok or exact_ok):
            return False
    return True


__all__ = [
    'RESULT_MIN_BM25', 'GATE_REFUSAL_NOTE', 'FilterOutcome', 'GateDecision',
    'unit_status', 'is_repealed', 'effective_from_ok', 'in_window', 'expired',
    'title_key', 'current_documents', 'is_stale',
    'table_header_only', 'heading_only', 'heading_flags', 'dedupe_heading_units',
    'apply_hard_filters', 'relevance_gate', 'passes_result_gate',
]
