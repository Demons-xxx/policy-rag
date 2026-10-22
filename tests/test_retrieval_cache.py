"""检索结果缓存（T9）：LRU 语义、缓存键、引擎二次命中。

用自造 release + 假编码器，不加载真实模型。
"""
from __future__ import annotations

from pathlib import Path

from app import config
from app.retrieval.cache import ResultCache, make_key, normalize_key_text
from app.retrieval.engine import PolicyRetriever
from tests.test_retrieval import FakeEncoder, build_release


def test_cache_get_returns_isolated_copy_and_stats() -> None:
    cache = ResultCache(size=2)
    cache.put(('k',), {'results': [{'id': 'a'}]})

    first = cache.get(('k',))
    assert first == {'results': [{'id': 'a'}]}
    first['results'].append({'id': 'b'})

    second = cache.get(('k',))
    assert second == {'results': [{'id': 'a'}]}  # 外部修改不污染缓存
    stats = cache.stats()
    assert stats['hits'] == 2 and stats['misses'] == 0
    assert cache.get(('missing',)) is None
    assert cache.stats()['misses'] == 1


def test_cache_lru_evicts_oldest() -> None:
    cache = ResultCache(size=2)
    cache.put(('a',), {'v': 1})
    cache.put(('b',), {'v': 2})
    assert cache.get(('a',)) is not None  # a 变成最近使用
    cache.put(('c',), {'v': 3})

    assert cache.get(('b',)) is None  # b 最久未用，被淘汰
    assert cache.get(('a',)) is not None
    assert cache.get(('c',)) is not None
    assert cache.size == 2


def test_make_key_normalizes_text_order_and_drops_empty_filters() -> None:
    left = make_key('rel-1', '  实验室  危化品 ', {'department': '', 'as_of': None}, '2026-10-22', 5)
    right = make_key('rel-1', '实验室 危化品', {}, '2026-10-22', 5)
    assert left == right

    ordered = make_key('rel-1', 'q', {'a': 1, 'b': 2}, '2026-10-22', 5)
    reversed_ = make_key('rel-1', 'q', {'b': 2, 'a': 1}, '2026-10-22', 5)
    assert ordered == reversed_


def test_make_key_distinguishes_release_top_k_and_as_of() -> None:
    base = make_key('rel-1', 'q', {'department': '财务处'}, '2026-10-22', 5)
    assert base != make_key('rel-2', 'q', {'department': '财务处'}, '2026-10-22', 5)
    assert base != make_key('rel-1', 'q', {'department': '财务处'}, '2026-10-22', 3)
    assert base != make_key('rel-1', 'q', {'department': '财务处'}, '2026-10-21', 5)
    assert base != make_key('rel-1', 'q', {'department': '人事处'}, '2026-10-22', 5)


def test_normalize_key_text_collapses_whitespace() -> None:
    assert normalize_key_text('  A\t b\n') == 'a b'


def test_engine_second_search_hits_cache(tmp_path: Path) -> None:
    retriever = PolicyRetriever(
        release_dir=build_release(tmp_path),
        backend='npz',
        device='cpu',
        encoder=FakeEncoder(),
    )
    events: list[tuple[str, dict]] = []

    first = retriever.search('实验室危险化学品怎么领用？', top_k=5, on_stage=lambda s, p: events.append((s, p)))
    assert first['cached'] is False
    assert first['results']

    events.clear()
    second = retriever.search('实验室危险化学品怎么领用？', top_k=5, on_stage=lambda s, p: events.append((s, p)))
    assert second['cached'] is True
    assert [item['unit']['id'] for item in second['results']] == [
        item['unit']['id'] for item in first['results']]
    assert second['context'] == first['context']
    assert any(stage == 'evidence' for stage, _ in events)  # 命中后仍推送证据事件
    assert retriever.cache.stats()['hits'] == 1

    # 换 top_k 视为不同问题，重新检索
    third = retriever.search('实验室危险化学品怎么领用？', top_k=3)
    assert third['cached'] is False


def test_engine_cache_respects_size_config(tmp_path: Path) -> None:
    retriever = PolicyRetriever(
        release_dir=build_release(tmp_path),
        backend='npz',
        device='cpu',
        encoder=FakeEncoder(),
    )
    assert retriever.cache.size == int(config.CACHE_SIZE)
