"""多通道候选融合：单元级记录 + 加权 RRF。

记录结构保持 dict（与 units.jsonl 一致的宽表风格），字段：
``channels``（命中通道集合）、``dense`` / ``bm25``（原始最高分）、
``rrf``（融合分）、``passage_id`` / ``bm25_passage_id``（用于取原文窗口）。
"""
from __future__ import annotations

from app.retrieval.channels import linked_passage

# RRF 平滑常数
RRF_K = 60

# RRF 通道权重：精确命中优先，保证「第十二条」这类问题稳定置顶
CHANNEL_WEIGHTS = {
    'BGE': 1.0,
    'BM25': 1.0,
    '条款号': 2.0,
    '文号': 1.5,
    '章节': 1.2,
    '标题': 0.8,
}
CHANNEL_ORDER = ['BGE', 'BM25', '文号', '条款号', '章节', '标题']
# 精确/半精确通道：命中这些通道的结果不受向量阈值限制
EXACT_CHANNELS = ('文号', '条款号', '章节', '标题')


def new_record() -> dict:
    return {
        'channels': set(),
        'dense': 0.0,
        'bm25': 0.0,
        'rrf': 0.0,
        'passage_id': None,
        'bm25_passage_id': None,
    }


def touch(records: dict[str, dict], unit_id: str) -> dict:
    """取（必要时新建）某单元的融合记录。"""
    record = records.get(unit_id)
    if record is None:
        record = new_record()
        records[unit_id] = record
    return record


def add_dense_candidates(records: dict[str, dict], hits: list[tuple[str, float]],
                         passage_map: dict[str, dict],
                         unit_map: dict[str, dict]) -> list[str]:
    """把 BGE 片段命中折成单元记录，返回按首次命中排序的单元 id 列表。"""
    dense_unit_ids: list[str] = []
    for passage_id, score in hits:
        passage = passage_map.get(passage_id)
        if not passage:
            continue
        unit_id = str(passage.get('unit_id') or '')
        if unit_id not in unit_map:
            continue
        record = touch(records, unit_id)
        record['channels'].add('BGE')
        if float(score) > record['dense'] or record['passage_id'] is None:
            record['dense'] = float(score)
            record['passage_id'] = passage_id
        if unit_id not in dense_unit_ids:
            dense_unit_ids.append(unit_id)
    return dense_unit_ids


def add_keyword_candidates(records: dict[str, dict], hits: list[tuple[str, float]],
                           passage_map: dict[str, dict],
                           unit_map: dict[str, dict]) -> list[str]:
    """把 BM25 片段命中折成单元记录，返回按首次命中排序的单元 id 列表。"""
    keyword_unit_ids: list[str] = []
    for passage_id, score in hits:
        passage = passage_map.get(passage_id)
        if not passage:
            continue
        unit_id = str(passage.get('unit_id') or '')
        if unit_id not in unit_map:
            continue
        record = touch(records, unit_id)
        record['channels'].add('BM25')
        if float(score) > record['bm25'] or record['bm25_passage_id'] is None:
            record['bm25'] = float(score)
            record['bm25_passage_id'] = passage_id
        if record['passage_id'] is None:
            record['passage_id'] = passage_id
        if unit_id not in keyword_unit_ids:
            keyword_unit_ids.append(unit_id)
    return keyword_unit_ids


def interleave(groups: list[list[str]]) -> list[str]:
    """按名次交错合并多个有序列表：q1#1, q2#1, q1#2, q2#2…，去重保留首次出现。"""
    merged: list[str] = []
    seen: set[str] = set()
    for rank in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if rank >= len(group):
                continue
            item = group[rank]
            if item in seen:
                continue
            seen.add(item)
            merged.append(item)
    return merged


def merge_hits(groups: list[list[tuple[str, float]]]) -> list[tuple[str, float]]:
    """多子句的片段命中：按名次交错保序，同一片段取最高分。"""
    best: dict[str, float] = {}
    order: list[str] = []
    for rank in range(max((len(group) for group in groups), default=0)):
        for group in groups:
            if rank >= len(group):
                continue
            passage_id, score = group[rank]
            best[passage_id] = max(best.get(passage_id, 0.0), float(score))
            if passage_id not in best or passage_id not in order:
                order.append(passage_id)
    return [(passage_id, best[passage_id]) for passage_id in order]


def merge_into(target: dict[str, dict], source: dict[str, dict],
               weight: float = 1.0) -> None:
    """把一次子检索的单元记录并入总记录：rrf 相加（可加权），dense/bm25 取最大。"""
    for unit_id, record in source.items():
        destination = target.get(unit_id)
        if destination is None:
            destination = new_record()
            target[unit_id] = destination
        destination['rrf'] += float(weight) * float(record.get('rrf') or 0.0)
        destination['dense'] = max(destination['dense'], float(record.get('dense') or 0.0))
        destination['bm25'] = max(destination['bm25'], float(record.get('bm25') or 0.0))
        destination['channels'] |= set(record.get('channels') or ())
        if destination['passage_id'] is None:
            destination['passage_id'] = record.get('passage_id')
        if destination['bm25_passage_id'] is None:
            destination['bm25_passage_id'] = record.get('bm25_passage_id')


def fuse(records: dict[str, dict], channel_lists: dict[str, list[str]],
         unit_map: dict[str, dict], unit_passages: dict[str, list[dict]],
         weights: dict[str, float] | None = None, k: int = RRF_K) -> None:
    """按通道顺序做加权 RRF；同一通道内重复单元只按首次出现计名次。"""
    weights = CHANNEL_WEIGHTS if weights is None else weights
    for channel, unit_ids in channel_lists.items():
        weight = weights.get(channel, 1.0)
        seen: set[str] = set()
        rank = 0
        for unit_id in unit_ids:
            if unit_id in seen:
                continue
            seen.add(unit_id)
            rank += 1
            unit = unit_map.get(unit_id)
            if unit is None:
                continue
            record = touch(records, unit_id)
            record['channels'].add(channel)
            if record['passage_id'] is None:
                passage_id = linked_passage(unit_id, unit_passages)
                if passage_id is not None:
                    record['passage_id'] = passage_id
            record['rrf'] += weight / (k + rank)


__all__ = [
    'RRF_K', 'CHANNEL_WEIGHTS', 'CHANNEL_ORDER', 'EXACT_CHANNELS',
    'new_record', 'touch', 'add_dense_candidates', 'add_keyword_candidates',
    'interleave', 'merge_hits', 'merge_into', 'fuse',
]
