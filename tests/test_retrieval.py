"""检索链路单测：用自造 release + 假编码器，不依赖真实模型与网络。

覆盖：RRF 融合排序、条款号精确命中、repealed 过滤、旧版本 notes、token 预算、
on_stage 回调、查询解析、BM25、npz/chroma 后端。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path

import numpy as np
import pytest

from app import config
from app.retrieval.backends import create_backend
from app.retrieval.engine import PolicyRetriever
from app.retrieval.lexical import PostingBM25, words
from app.retrieval.query import chinese_to_int, expand_terms, parse_query

# ---------------------------------------------------------------------------
# 假编码器：按关键词映射到 4 维轴向量，便于断言排序
# ---------------------------------------------------------------------------

KEYWORD_VECTORS = {
    '实验室': (1.0, 0.0, 0.0, 0.0),
    '危险化学品': (1.0, 0.0, 0.0, 0.0),
    '差旅': (0.0, 1.0, 0.0, 0.0),
    '住宿': (0.0, 1.0, 0.0, 0.0),
    '报销': (0.0, 1.0, 0.0, 0.0),
    '请假': (0.0, 0.0, 1.0, 0.0),
    '长文本': (0.0, 0.0, 0.0, 1.0),
}


class FakeEncoder:
    """模拟 BgeEncoder：不加载模型、不联网，编码结果可预测。"""

    device = 'cpu'
    dimension = 4

    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bool]] = []

    def encode(self, texts: list[str], query: bool = False,
               batch_size: int = 32) -> np.ndarray:
        self.calls.append((list(texts), bool(query)))
        output = np.zeros((len(texts), self.dimension), dtype=np.float32)
        for index, text in enumerate(texts):
            value = np.zeros(self.dimension, dtype=np.float32)
            for keyword, axis in KEYWORD_VECTORS.items():
                if keyword in str(text):
                    value += np.asarray(axis, dtype=np.float32)
            norm = float(np.linalg.norm(value))
            if norm == 0.0:
                # 无关键词时给一个很小的均匀向量，保证有限且可比
                value = np.full(self.dimension, 0.01, dtype=np.float32)
                norm = float(np.linalg.norm(value))
            output[index] = value / norm
        return output


# ---------------------------------------------------------------------------
# 自造发布目录
# ---------------------------------------------------------------------------

def _unit(unit_id: str, doc_id: str, title: str, **overrides) -> dict:
    unit = {
        'id': unit_id,
        'doc_id': doc_id,
        'doc_title': title,
        'doc_no': None,
        'version_label': None,
        'effective_from': '2024-07-01',
        'effective_to': None,
        'department': None,
        'status': 'active',
        'chapter': None,
        'section': None,
        'article': None,
        'article_no': None,
        'kind': 'article',
        'heading_path': [],
        'text': '',
        'pages': [1],
        'table': None,
        'order': 1,
    }
    unit.update(overrides)
    unit['heading_path'] = [item for item in (unit.get('chapter'), unit.get('article')) if item]
    page = unit['pages'][0] if unit.get('pages') else 1
    unit['source'] = {
        'doc_id': doc_id,
        'file': overrides.get('file', f'{title}.pdf'),
        'page': page,
        'page_end': page,
        'parser': 'pymupdf',
    }
    return unit


def _units() -> list[dict]:
    long_text = '长文本测试条款内容。' * 250  # 2500 字，用于验证片段截断
    return [
        _unit(
            'doc-a#u0001', 'doc-a', 'XX大学实验室安全管理办法',
            doc_no='校发〔2024〕12号', version_label='2024修订版',
            effective_from='2024-07-01', department='实验室与设备管理处',
            chapter='第三章 危险化学品管理', article='第十二条', article_no=12,
            text='第十二条 危险化学品领用应当由实验室负责人审批，双人领用、双人保管。',
            pages=[3], order=7,
        ),
        _unit(
            'doc-a#u0002', 'doc-a', 'XX大学实验室安全管理办法',
            doc_no='校发〔2024〕12号', version_label='2024修订版',
            effective_from='2024-07-01', department='实验室与设备管理处',
            chapter='第三章 危险化学品管理', article=None, article_no=None,
            kind='section', text='第三章 危险化学品管理', pages=[3], order=6,
        ),
        _unit(
            'doc-old#u0001', 'doc-old', 'XX大学差旅费管理办法',
            doc_no='校发〔2020〕5号', version_label='2020版', status='superseded',
            effective_from='2020-01-01', effective_to='2025-08-31',
            chapter='第二章 开支标准', article='第五条', article_no=5,
            text='差旅住宿标准为每人每天300元。', pages=[2], order=3,
        ),
        _unit(
            'doc-new#u0001', 'doc-new', 'XX大学差旅费管理办法（2025年修订）',
            doc_no='校发〔2025〕8号', version_label='2025修订版', status='active',
            effective_from='2025-09-01', effective_to=None,
            chapter='第二章 开支标准', article='第五条', article_no=5,
            text='差旅住宿标准为每人每天500元。', pages=[2], order=3,
        ),
        _unit(
            'doc-repealed#u0001', 'doc-repealed', 'XX大学差旅报销暂行办法',
            doc_no='校发〔2015〕2号', version_label='2015版', status='repealed',
            effective_from='2015-01-01', effective_to='2019-12-31',
            chapter='第二章 报销流程', article='第三条', article_no=3,
            text='差旅报销需提供纸质发票。', pages=[1], order=2,
        ),
        _unit(
            'doc-leave#u0001', 'doc-leave', 'XX大学教职工请假管理办法',
            doc_no='校发〔2023〕9号', version_label='2023版', status='active',
            effective_from='2023-03-01', department='人事处',
            chapter='第三章 审批权限', article='第七条', article_no=7,
            text='请假三天以内由所在部门负责人审批。', pages=[4], order=5,
        ),
        _unit(
            'doc-big#u0001', 'doc-big', 'XX大学长文本测试办法',
            doc_no='校发〔2022〕3号', version_label='2022版', status='active',
            effective_from='2022-01-01', chapter='第一章 总则',
            article='第一条', article_no=1, text=long_text, pages=[1], order=1,
        ),
    ]


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.write_text(
        '\n'.join(json.dumps(row, ensure_ascii=False) for row in rows) + '\n',
        encoding='utf-8',
    )


def build_release(base: Path) -> Path:
    """构造一个最小发布目录（units/passages/vectors/manifest）。"""
    release_dir = base / 'rel-test'
    release_dir.mkdir(parents=True, exist_ok=True)
    units = _units()

    passages: list[dict] = []
    for index, unit in enumerate(units, start=1):
        text = unit['text']
        passages.append({
            'id': f'p_{index:06d}',
            'unit_id': unit['id'],
            'text': text,
            'embedding_text': (
                f"{unit['doc_title']} {unit.get('article') or unit.get('chapter') or ''} {text}"
            ),
            'start': 0,
            'end': len(text),
        })

    _write_jsonl(release_dir / 'units.jsonl', units)
    _write_jsonl(release_dir / 'passages.jsonl', passages)

    encoder = FakeEncoder()
    vectors = encoder.encode([passage['text'] for passage in passages])
    np.savez(
        release_dir / 'vectors.npz',
        ids=np.array([passage['id'] for passage in passages]),
        vectors=vectors.astype(np.float32),
    )

    manifest = {
        'schema': 1,
        'release_id': 'rel-test',
        'published_at': '2026-10-21T15:30:00+08:00',
        'doc_count': len({unit['doc_id'] for unit in units}),
        'unit_count': len(units),
        'passage_count': len(passages),
        'embedding': {'model': 'fake-bge', 'dimension': 4, 'query_prefix': '为这个句子生成表示以用于检索相关文章：'},
        'collection': 'policy_rel-test',
    }
    (release_dir / 'manifest.json').write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8'
    )
    return release_dir


@pytest.fixture(scope='module')
def release_dir(tmp_path_factory) -> Path:
    return build_release(tmp_path_factory.mktemp('release'))


@pytest.fixture()
def retriever(release_dir: Path) -> PolicyRetriever:
    return PolicyRetriever(
        release_dir=release_dir,
        backend='npz',
        device='cpu',
        encoder=FakeEncoder(),
    )


def _ids(result: dict) -> list[str]:
    return [item['unit']['id'] for item in result['results']]


# ---------------------------------------------------------------------------
# 检索行为
# ---------------------------------------------------------------------------

def test_fusion_ranking_and_citation(retriever: PolicyRetriever) -> None:
    result = retriever.search('实验室危险化学品怎么领用？', top_k=5)

    assert result['results'], '应至少召回一条证据'
    top = result['results'][0]
    assert top['unit']['id'] == 'doc-a#u0001'
    assert 'BGE' in top['channels'] and 'BM25' in top['channels']
    assert top['scores']['rrf'] > 0
    assert top['citation'] == {
        'doc_id': 'doc-a',
        'title': 'XX大学实验室安全管理办法',
        'doc_no': '校发〔2024〕12号',
        'article': '第十二条',
        'chapter': '第三章 危险化学品管理',
        'page': 3,
        'pages': [3],
        'version_label': '2024修订版',
        'file': 'XX大学实验室安全管理办法.pdf',
    }
    assert '第三章' in top['evidence'] and '第十二条' in top['evidence']
    # 章节导语在同一章节正文已召回时被去重
    assert 'doc-a#u0002' not in _ids(result)
    # context 编号与 results 对齐
    assert result['context'].startswith('[证据1] ')
    assert result['tokens'] > 0
    assert result['filters']['as_of']


def test_article_number_exact_hit(retriever: PolicyRetriever) -> None:
    result = retriever.search('第十二条有哪些规定？', top_k=3)

    assert result['results'][0]['unit']['id'] == 'doc-a#u0001'
    assert '条款号' in result['results'][0]['channels']


def test_repealed_filtered_and_old_version_noted(retriever: PolicyRetriever) -> None:
    result = retriever.search('差旅报销有什么标准？', top_k=5)
    ids = _ids(result)

    # repealed 文档绝不返回
    assert 'doc-repealed#u0001' not in ids
    assert all(item['unit'].get('status') != 'repealed' for item in result['results'])
    # 新旧两版同标题：只返回现行版正文，旧版不再作为证据
    assert 'doc-new#u0001' in ids
    assert 'doc-old#u0001' not in ids
    # 普通问题不主动提示版本，避免每次回答都带一句无关的版本说明
    assert not any('旧版内容仅作参考' in note for note in result['notes'])


def test_version_question_gets_note(retriever: PolicyRetriever) -> None:
    """用户明确问旧版/有效性时，即使旧版没进证据也要提示现行版本。"""
    result = retriever.search('差旅报销旧版标准还有效吗？', top_k=5)
    ids = _ids(result)

    assert 'doc-old#u0001' not in ids
    assert any('旧版内容仅作参考' in note for note in result['notes'])


def test_as_of_filters_future_version(retriever: PolicyRetriever) -> None:    # 2024 年时点：新版本尚未施行，应只命中旧版且不产生旧版提示
    result = retriever.search('差旅住宿标准', top_k=5, filters={'as_of': '2024-01-01'})
    ids = _ids(result)

    assert 'doc-old#u0001' in ids
    assert 'doc-new#u0001' not in ids
    assert not any('旧版' in note for note in result['notes'])


def test_version_note_even_if_old_not_selected(retriever: PolicyRetriever) -> None:
    """旧版没进证据时也要提示存在更新版本，避免用户误用旧标准。"""
    result = retriever.search('差旅报销旧版标准还有效吗？', top_k=1)

    assert len(result['results']) == 1
    assert result['results'][0]['unit']['id'] == 'doc-new#u0001'
    assert any('旧版内容仅作参考' in note for note in result['notes'])


def test_token_budget_truncates_results(retriever: PolicyRetriever,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(config, 'MAX_CONTEXT_TOKENS', 60)
    result = retriever.search('差旅请假', top_k=5)

    assert len(result['results']) == 1, '预算只够一条证据'
    assert result['tokens'] <= 60
    assert any('超出上下文预算' in note for note in result['notes'])


def test_long_unit_uses_excerpt(retriever: PolicyRetriever) -> None:
    result = retriever.search('长文本测试条款内容', top_k=3)

    top = result['results'][0]
    assert top['unit']['id'] == 'doc-big#u0001'
    # 超长单元只保留命中片段（约 1200 token），不再整段塞入
    assert len(top['evidence']) <= 1300
    assert len(top['evidence']) < len(top['unit']['text'])


def test_on_stage_callback(retriever: PolicyRetriever) -> None:
    stages: list[tuple[str, dict]] = []
    result = retriever.search(
        '实验室危险化学品怎么领用',
        top_k=3,
        on_stage=lambda stage, payload: stages.append((stage, payload)),
    )

    assert [stage for stage, _ in stages] == ['query', 'vectors', 'keywords', 'evidence']
    assert stages[0][1]['rewritten']
    assert stages[1][1]['passage_count'] > 0
    assert stages[2][1]['passage_count'] > 0
    assert stages[3][1]['titles'] == [
        f'《{item["unit"]["doc_title"]}》{item["unit"].get("article") or ""}'.strip()
        for item in result['results']
    ]
    assert stages[3][1]['notes'] == result['notes']


def test_info(retriever: PolicyRetriever) -> None:
    info = retriever.info()
    assert info['release_id'] == 'rel-test'
    assert info['backend'] == 'npz'
    assert info['unit_count'] == 7
    assert info['passage_count'] == 7
    assert info['dimension'] == 4


# ---------------------------------------------------------------------------
# 查询解析 / 分词 / BM25
# ---------------------------------------------------------------------------

def test_parse_query_structures() -> None:
    parsed = parse_query('关于印发《XX办法》的通知（校发〔2024〕12号）第十二条')
    assert parsed['doc_no'] == '校发〔2024〕12号'
    assert parsed['article'] == '第十二条'
    assert parsed['article_no'] == 12
    assert '实验室' not in parsed['keywords']
    assert parsed['is_followup'] is False

    followup = parse_query('那第十二条呢')
    assert followup['article_no'] == 12
    assert followup['is_followup'] is True

    chapter = parse_query('第三章 第二节讲了什么')
    assert chapter['chapter'] == '第三章'
    assert chapter['section'] == '第二节'

    arabic = parse_query('第3条')
    assert arabic['article'] == '第3条' and arabic['article_no'] == 3


def test_department_and_expansion() -> None:
    parsed = parse_query('请问教务处的报销规定是什么')
    assert parsed['department'] == '教务处'

    expanded = expand_terms(['报销'])
    assert '报销' in expanded and '费用报销' in expanded and '经费' in expanded


def test_chinese_numbers() -> None:
    assert chinese_to_int('十二') == 12
    assert chinese_to_int('二十') == 20
    assert chinese_to_int('一百零三') == 103
    assert chinese_to_int('12') == 12
    assert chinese_to_int('') is None


def test_words_and_bm25() -> None:
    tokens = words('实验室危险化学品ABC-123怎么领用')
    assert '实验室' in tokens and 'abc' in tokens and '123' in tokens

    bm25 = PostingBM25(['a', 'b'], [['报销', '标准'], ['请假', '审批']])
    scores = bm25.get_scores(['报销', '不存在的词'])
    assert scores[0] > scores[1] == 0.0
    empty = PostingBM25([], [])
    assert empty.get_scores(['报销']).shape == (0,)
    assert empty.get_scores(['报销']).dtype == np.float32


# ---------------------------------------------------------------------------
# 向量后端
# ---------------------------------------------------------------------------

def test_npz_backend(release_dir: Path) -> None:
    backend = create_backend('npz', release_dir, 'policy_rel-test')
    assert backend.count() == 7

    vector = np.array([1.0, 0.0, 0.0, 0.0], dtype=np.float32)
    hits = backend.search(vector, 3)
    assert hits and hits[0][0] in {'p_000001', 'p_000002'}
    assert {passage_id for passage_id, _ in hits[:2]} == {'p_000001', 'p_000002'}
    assert hits[0][1] >= hits[1][1] > 0.99
    assert hits[2][1] <= 0.5

    vectors = backend.vectors_for(['p_000001', 'missing'])
    assert set(vectors) == {'p_000001'}
    assert vectors['p_000001'].shape == (4,)
    # 只读后端：add/reset 为空实现，不改变数据
    backend.add([], np.zeros((0, 4), dtype=np.float32), [])
    backend.reset()
    assert backend.count() == 7


def test_chroma_backend(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    pytest.importorskip('chromadb')
    monkeypatch.setattr(config, 'VECTOR_DIR', tmp_path / 'vector_store')
    collection = f'test_{uuid.uuid4().hex[:8]}'
    backend = create_backend('chroma', tmp_path / 'unused-release', collection)
    try:
        vectors = np.array([[1.0, 0.0], [0.0, 1.0]], dtype=np.float32)
        backend.add(['a', 'b'], vectors, [{'doc_id': 'a'}, {'doc_id': 'b'}])
        assert backend.count() == 2

        hits = backend.search(np.array([1.0, 0.0], dtype=np.float32), 2)
        assert hits[0][0] == 'a'
        assert hits[0][1] > 0.9

        filtered = backend.search(
            np.array([1.0, 0.0], dtype=np.float32), 2,
            where={'doc_id': {'$in': ['b']}},
        )
        assert [passage_id for passage_id, _ in filtered] == ['b']

        found = backend.vectors_for(['a'])
        assert found['a'].shape == (2,)

        backend.reset()
        assert backend.count() == 0
    finally:
        try:
            backend.client.delete_collection(name=collection)
        except Exception:
            pass


def test_rerank_reorders_candidates_and_records_scores(monkeypatch):
    from app.retrieval import engine

    class FakeReranker:
        def score(self, question, texts, batch_size=None):
            return [0.1, 0.9, 0.5, 0.2][:len(texts)]

    monkeypatch.setattr('app.retrieval.rerank.get_reranker', lambda: FakeReranker())
    units = {key: {'title': '制度', 'article': '第一条', 'text': key} for key in ('a', 'b', 'c', 'd')}
    records = {key: {} for key in units}
    ordered = engine._apply_rerank('问题', ['a', 'b', 'c', 'd'], units, records)
    assert ordered == ['b', 'c', 'd', 'a']
    assert records['b']['scores']['rerank'] == 0.9


def test_rerank_falls_back_when_model_missing(monkeypatch):
    from app.retrieval import engine

    monkeypatch.setattr('app.retrieval.rerank.get_reranker', lambda: None)
    units = {key: {'text': key} for key in ('a', 'b')}
    assert engine._apply_rerank('问题', ['a', 'b'], units, {}) == ['a', 'b']
