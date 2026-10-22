"""排序与数量控制：旧版降权、章节导语降权、跨文档多样性、表格挂接占位、notes 合并。

排序键与选择规则都是纯函数，输入融合记录与过滤结果，输出有序的 unit_id 列表。
"""
from __future__ import annotations

import re
from typing import Callable

# 同标题旧版本降权但不驱逐：正常提问由现行版胜出，用户明确点名旧文号时旧版
# 仍可能排前（同时会在 notes 提示）
STALE_FACTOR = 0.7
# 纯章节导语保留但压低权重，避免抢占正文位置
HEADING_FACTOR = 0.8
# 版本提示的入选门槛：旧版候选得分达到最佳证据的该比例才提示
VERSION_NOTE_RATIO = 0.6
# 跨文档换入门槛：另一份文档的最优候选得分需达到当前最佳的该比例
DIVERSITY_RATIO = 0.5


def dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def order_key(record: dict, unit_id: str, stale: bool, heading: bool) -> tuple:
    score = float(record['rrf'])
    if stale:
        score *= STALE_FACTOR
    if heading:
        score *= HEADING_FACTOR
    return (
        -score,
        1 if stale else 0,
        1 if heading else 0,
        -float(record['dense']),
        -float(record['bm25']),
        unit_id,
    )


def order_units(kept: list[str], records: dict[str, dict],
                stale_ids: set[str], heading_flags: dict[str, bool]) -> list[str]:
    """按「降权后 RRF → 新旧 → 是否导语 → 原始分 → id」稳定排序。"""
    return sorted(
        kept,
        key=lambda unit_id: order_key(
            records[unit_id],
            unit_id,
            unit_id in stale_ids,
            bool(heading_flags.get(unit_id)),
        ),
    )


def diversify_documents(ordered: list[str], top_k: int,
                        unit_map: dict[str, dict],
                        records: dict[str, dict]) -> list[str]:
    """跨制度问题：前几名全来自同一文档时，用最后一个名额换入另一份文档的最优候选。"""
    selection = list(ordered[:top_k])
    if len(selection) >= 3:
        selected_docs = {str(unit_map[uid].get('doc_id')) for uid in selection}
        if len(selected_docs) == 1:
            best_score = float(records[selection[0]]['rrf'])
            for uid in ordered[top_k:]:
                if str(unit_map[uid].get('doc_id')) in selected_docs:
                    continue
                if float(records[uid]['rrf']) >= best_score * DIVERSITY_RATIO:
                    selection[-1] = uid
                    break
    return selection


def _unit_text(unit: dict) -> str:
    """单元可比对文本：正文 + 表格单元格 + 章节/条款抬头（去空白）。"""
    parts = [str(unit.get('text') or '')]
    table = unit.get('table') or {}
    parts.extend(str(cell) for cell in (table.get('headers') or []))
    parts.extend(str(cell) for cell in (table.get('row') or []))
    for key in ('chapter', 'section', 'article'):
        parts.append(str(unit.get(key) or ''))
    return re.sub(r'\s+', '', ' '.join(part for part in parts if part))


def prefer_numeric_units(ordered: list[str], tokens: list[str],
                         unit_map: dict[str, dict], window: int = 20) -> list[str]:
    """问题含数值/期限时，把命中该数值的单元提到窗口前部（保持原有相对顺序）。"""
    if not tokens or not ordered:
        return list(ordered)
    head = ordered[:max(1, window)]
    tail = ordered[max(1, window):]
    hit: list[str] = []
    rest: list[str] = []
    for unit_id in head:
        text = _unit_text(unit_map[unit_id])
        if any(token in text for token in tokens):
            hit.append(unit_id)
        else:
            rest.append(unit_id)
    return hit + rest + tail


def merge_article_units(ordered: list[str], unit_map: dict[str, dict]) -> tuple[list[str], dict[str, list[dict]]]:
    """同一 article 的多个单元合并成一个证据位：保留最先出现的，其余作为附加片段。

    返回 ``(去重后的顺序, {代表 unit_id: [附加单元, ...]})``；
    没有 article（表格行、章节导语等）的单元不参与合并。
    """
    kept: list[str] = []
    seen: dict[tuple, str] = {}
    merged: dict[str, list[dict]] = {}
    for unit_id in ordered:
        unit = unit_map[unit_id]
        article = str(unit.get('article') or '').strip()
        if not article:
            kept.append(unit_id)
            continue
        key = (unit.get('doc_id'), unit.get('chapter'), unit.get('section'), article)
        first = seen.get(key)
        if first is None:
            seen[key] = unit_id
            kept.append(unit_id)
            continue
        merged.setdefault(first, []).append(unit)
    return kept, merged


def fill_with_attached_rows(selection: list[str], ordered: list[str], top_k: int,
                            unit_map: dict[str, dict],
                            adjacent_rows: Callable[[dict], list[dict]]) -> list[str]:
    """条款证据会带上紧邻的表格行；这些行不再单独占一个证据位。"""
    chosen: list[str] = []
    attached_rows: set[str] = set()
    for uid in list(selection) + [item for item in ordered if item not in selection]:
        if len(chosen) >= top_k:
            break
        if uid in attached_rows:
            continue
        chosen.append(uid)
        unit = unit_map[uid]
        if str(unit.get('kind') or '') == 'article':
            for row in adjacent_rows(unit):
                attached_rows.add(str(row.get('id')))
    return chosen


def enforce_quotas(selection: list[str], ordered: list[str],
                   quotas: list[list[str]], top_k: int) -> list[str]:
    """一句多问时保证每个子问题的最优证据至少占一个名额。

    ``quotas`` 是每个子问题各自的高分单元（按子问题内得分从高到低）。
    只认每个子问题的第一名：已入选就跳过，否则换掉全局排序里最靠后、
    又不是其它子问题第一名的证据位。这样「采购」「报销」各自的最佳条款都会出现。
    """
    chosen = list(selection)
    selected = set(chosen)
    targets = [group[0] for group in quotas if group]
    protected = set(targets)
    for target in targets:
        if target in selected:
            continue
        if len(chosen) < top_k:
            chosen.append(target)
            selected.add(target)
            continue
        victim = None
        for uid in reversed(chosen):
            if uid not in protected:
                victim = uid
                break
        if victim is None:
            continue
        chosen[chosen.index(victim)] = target
        selected.discard(victim)
        selected.add(target)
    return sorted(chosen, key=lambda uid: ordered.index(uid) if uid in ordered else len(ordered))


def best_selected_score(results: list[dict], records: dict[str, dict]) -> float:
    best = 0.0
    for item in results:
        value = float(records[str(item['unit']['id'])]['rrf'])
        if value > best:
            best = value
    return best


def version_note_score_ok(score: float, best_selected: float,
                          ratio: float = VERSION_NOTE_RATIO) -> bool:
    """旧版候选与最佳证据得分接近时才提示，避免每次都附带版本说明。"""
    if best_selected and score < best_selected * ratio:
        return False
    return True


def note_over_budget(title: str, budget: int) -> str:
    return f'《{title}》证据超出上下文预算（{budget} tokens），已省略'


def merge_notes(version_notes: list[str], notes: list[str]) -> list[str]:
    """版本提示放在预算提示之前，更符合阅读顺序。"""
    return dedupe(version_notes) + dedupe(notes)


__all__ = [
    'STALE_FACTOR', 'HEADING_FACTOR', 'VERSION_NOTE_RATIO', 'DIVERSITY_RATIO',
    'dedupe', 'order_key', 'order_units', 'diversify_documents',
    'prefer_numeric_units', 'merge_article_units',
    'fill_with_attached_rows', 'enforce_quotas', 'best_selected_score', 'version_note_score_ok',
    'note_over_budget', 'merge_notes',
]
