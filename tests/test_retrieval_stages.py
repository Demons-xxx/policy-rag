"""检索阶段单测：通道 / 融合 / 过滤 / 排序 / 证据。

只测阶段纯函数，不碰真实模型与网络；端到端行为由 tests/test_retrieval.py 覆盖。
样例数据在本文件内联构造（tests/ 下原有文件与 conftest 不改动）。
"""
from __future__ import annotations

import numpy as np
import pytest

from app.retrieval import channels, evidence, filters, fusion, ranking
from app.retrieval.lexical import PostingBM25


# ---------------------------------------------------------------------------
# 样例数据
# ---------------------------------------------------------------------------

def unit(unit_id: str, doc_id: str, title: str, **overrides) -> dict:
    row = {
        'id': unit_id,
        'doc_id': doc_id,
        'doc_title': title,
        'doc_no': None,
        'version_label': None,
        'effective_from': '2024-01-01',
        'effective_to': None,
        'department': '教务处',
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
    row.update(overrides)
    return row


def unit_map_of(rows: list[dict]) -> dict[str, dict]:
    return {str(row['id']): row for row in rows}


class FakeBackend:
    def __init__(self) -> None:
        self.calls: list[dict] = []

    def search(self, vector, top_k, where=None):
        self.calls.append({'top_k': top_k, 'where': where})
        return [('p1', 0.7)]


class FakeEncoder:
    device = 'cpu'

    def encode(self, texts, query=False, batch_size=32):
        return np.ones((len(texts), 4), dtype=np.float32)


class BrokenEncoder:
    device = 'cpu'

    def encode(self, texts, query=False, batch_size=32):
        raise RuntimeError('模型未就绪')


# ---------------------------------------------------------------------------
# 1. 通道：四路精确/半精确召回
# ---------------------------------------------------------------------------

def test_exact_channels_doc_no_article_chapter_title() -> None:
    rows = [
        unit('u2', 'd1', '差旅费管理办法', doc_no='校发〔2024〕1号',
             chapter='第二章 开支标准', article='第五条', article_no=5, order=2),
        unit('u1', 'd1', '差旅费管理办法', doc_no='校发〔2024〕1号',
             chapter='第一章 总则', article='第一条', article_no=1, order=1),
        unit('u3', 'd2', '差旅费管理办法（2025年修订）', doc_no='校发〔2025〕9号',
             chapter='第二章 开支标准', article='第五条', article_no=5, order=1),
    ]
    units, unit_map = rows, unit_map_of(rows)

    by_doc_no = channels.exact_channels(units, unit_map, {'doc_no': '校发〔2024〕1号'})
    assert by_doc_no == {'文号': ['u1', 'u2']}, '同文号单元按原文 order 排序'

    by_article = channels.exact_channels(units, unit_map, {'article_no': 5, 'article': '第五条'})
    assert by_article['条款号'] == ['u3', 'u2']

    by_chapter = channels.exact_channels(units, unit_map, {'chapter': '第二章'})
    assert by_chapter['章节'] == ['u3', 'u2'], '章节号相同即命中，与章节标题文字无关'

    by_title = channels.exact_channels(units, unit_map, {'keywords': ['差旅费', '不存在的词']})
    assert set(by_title['标题']) == {'u1', 'u2', 'u3'}

    assert channels.exact_channels(units, unit_map, {}) == {}


# ---------------------------------------------------------------------------
# 2. 融合：加权 RRF
# ---------------------------------------------------------------------------

def test_fusion_rrf_weights() -> None:
    rows = [unit('u1', 'd1', '甲办法'), unit('u2', 'd2', '乙办法')]
    unit_map = unit_map_of(rows)
    passages = {
        'p1': {'id': 'p1', 'unit_id': 'u1', 'start': 0},
        'p2': {'id': 'p2', 'unit_id': 'u2', 'start': 0},
    }
    unit_passages = {'u1': [passages['p1']], 'u2': [passages['p2']]}

    records: dict[str, dict] = {}
    dense_ids = fusion.add_dense_candidates(records, [('p1', 0.9)], passages, unit_map)
    keyword_ids = fusion.add_keyword_candidates(records, [('p2', 6.5)], passages, unit_map)
    assert dense_ids == ['u1'] and keyword_ids == ['u2']
    assert records['u1']['passage_id'] == 'p1'
    assert records['u2']['bm25_passage_id'] == 'p2'

    fusion.fuse(records, {'BGE': dense_ids, 'BM25': keyword_ids,
                          '条款号': ['u2'], '文号': ['u1', 'u2']},
                unit_map, unit_passages)

    # 条款号权重 2.0、文号 1.5，BGE/BM25 各 1.0；名次从 1 开始
    assert records['u1']['rrf'] == pytest.approx(1.0 / 61 + 1.5 / 61)
    assert records['u2']['rrf'] == pytest.approx(1.0 / 61 + 2.0 / 61 + 1.5 / 62)
    assert records['u1']['channels'] == {'BGE', '文号'}
    assert records['u2']['channels'] == {'BM25', '条款号', '文号'}


# ---------------------------------------------------------------------------
# 3. 通道：向量降级与 BM25 召回
# ---------------------------------------------------------------------------

def test_dense_fallback_and_keyword_candidates() -> None:
    backend = FakeBackend()
    hits, note = channels.dense_candidates(FakeEncoder(), backend, '报销', doc_filter='d1')
    assert hits == [('p1', 0.7)] and note is None
    assert backend.calls[0]['where'] == {'doc_id': {'$in': ['d1']}}
    assert backend.calls[0]['top_k'] == channels.DENSE_TOP

    empty, empty_note = channels.dense_candidates(BrokenEncoder(), backend, '报销')
    assert empty == [] and '向量检索不可用' in empty_note

    bm25 = PostingBM25(['p1', 'p2'], [['报销', '标准'], ['请假']])
    keyword_hits, keyword_note = channels.keyword_candidates(bm25, ['p1', 'p2'], '报销标准')
    assert keyword_note is None
    assert keyword_hits and keyword_hits[0][0] == 'p1'
    assert all(score > 0 for _, score in keyword_hits)

    # 精确通道只给 unit_id 时，用首个片段挂接原文窗口
    assert channels.linked_passage('u1', {'u1': [{'id': 'p1'}, {'id': 'p2'}]}) == 'p1'
    assert channels.linked_passage('missing', {}) is None


# ---------------------------------------------------------------------------
# 4. 过滤：状态 / 时效 / 表头 / 部门 / 旧版 / 导语去重
# ---------------------------------------------------------------------------

def test_hard_filters_and_heading_dedupe() -> None:
    rows = [
        unit('now', 'd-new', '差旅费管理办法', order=1),
        unit('old', 'd-old', '差旅费管理办法', doc_no='校发〔2020〕5号',
             status='superseded', effective_from='2020-01-01',
             effective_to='2022-12-31', order=2),
        unit('future', 'd-future', '采购管理办法', effective_from='2030-01-01', order=3),
        unit('dead', 'd-dead', '旧办法', status='repealed', order=4),
        unit('header', 'd-tab', '表格办法', kind='table_row', order=5,
             table={'headers': ['项目', '标准'], 'row': ['项目', '标准']}),
        unit('other-dept', 'd-other', '其他办法', department='人事处', order=6),
        unit('sec', 'd-sec', '会议办法', kind='section', chapter='第一章 总则',
             text='第一章 总则', order=7),
        unit('sec-body', 'd-sec', '会议办法', chapter='第一章 总则',
             article='第一条', text='第一条 会议室须预约。', order=8),
    ]
    unit_map = unit_map_of(rows)
    records = {row['id']: {'rrf': 0.01, 'dense': 0.0, 'bm25': 0.0}
               for row in rows}
    as_of = '2025-06-01'
    current = filters.current_documents(rows, as_of)
    assert current[versions_title('差旅费管理办法')] == 'd-new'

    outcome = filters.apply_hard_filters(records, unit_map, current, as_of)
    assert outcome.usable == ['now', 'other-dept', 'sec', 'sec-body']
    assert outcome.stale_ids == {'old'}
    assert [uid for uid, _ in outcome.stale_candidates] == ['old']

    # 用户明确点名旧文号时，旧版放行但仍记入 stale
    named = filters.apply_hard_filters(
        records, unit_map, current, as_of,
        explicit_doc_key=filters.doc_no_key('校发〔2020〕5号'))
    assert 'old' in named.usable and named.stale_ids == {'old'}

    # 部门 / 文档硬过滤
    dept = filters.apply_hard_filters(
        records, unit_map, current, as_of, department_filter='人事处')
    assert dept.usable == ['other-dept']
    doc = filters.apply_hard_filters(
        records, unit_map, current, as_of, doc_filter='d-new')
    assert doc.usable == ['now']

    # 章节导语：同章节已有正文时不再占位
    flags = filters.heading_flags(outcome.usable, unit_map)
    assert flags['sec'] is True and flags['sec-body'] is False
    assert filters.dedupe_heading_units(outcome.usable, flags, unit_map) == ['now', 'other-dept', 'sec-body']


def versions_title(title: str) -> str:
    from app.domain import versions
    return versions.title_key({'doc_title': title})


# ---------------------------------------------------------------------------
# 5. 过滤：相关性门控
# ---------------------------------------------------------------------------

def test_relevance_gate_and_result_gate() -> None:
    low = [('p1', 0.2)]
    blocked = filters.relevance_gate(low, {}, 0.45)
    assert blocked.blocked is True and blocked.max_dense == pytest.approx(0.2)

    # 有精确命中时不触发拒答
    assert filters.relevance_gate(low, {'文号': ['u1']}, 0.45).blocked is False
    # 阈值 0 关闭
    assert filters.relevance_gate(low, {}, 0.0).blocked is False
    # 高向量分不触发
    assert filters.relevance_gate([('p1', 0.8)], {}, 0.45).blocked is False

    weak = {'dense': 0.1, 'bm25': 1.0, 'channels': set()}
    assert filters.passes_result_gate(weak, 0.45, 0.9) is False
    assert filters.passes_result_gate({'dense': 0.6, 'bm25': 0.0, 'channels': set()},
                                      0.45, 0.9) is True
    assert filters.passes_result_gate({'dense': 0.0, 'bm25': 9.0, 'channels': set()},
                                      0.45, 0.9) is True
    assert filters.passes_result_gate({'dense': 0.0, 'bm25': 0.0, 'channels': {'条款号'}},
                                      0.45, 0.9) is True
    # max_dense 为 0（无向量结果）时不做单条门槛
    assert filters.passes_result_gate(weak, 0.45, 0.0) is True


# ---------------------------------------------------------------------------
# 6. 排序：降权、跨文档多样性、表格占位
# ---------------------------------------------------------------------------

def test_ranking_downweight_diversity_and_row_placeholder() -> None:
    rows = [
        unit('a1', 'd1', '甲办法'), unit('a2', 'd1', '甲办法'),
        unit('a3', 'd1', '甲办法'), unit('b1', 'd2', '乙办法'),
        unit('old', 'd0', '丙办法'), unit('head', 'd3', '丁办法', kind='section'),
    ]
    unit_map = unit_map_of(rows)
    records = {
        'a1': {'rrf': 0.050, 'dense': 0.9, 'bm25': 6.0},
        'a2': {'rrf': 0.040, 'dense': 0.8, 'bm25': 5.0},
        'a3': {'rrf': 0.030, 'dense': 0.7, 'bm25': 4.0},
        'b1': {'rrf': 0.026, 'dense': 0.0, 'bm25': 9.0},
        'old': {'rrf': 0.055, 'dense': 0.0, 'bm25': 0.0},
        'head': {'rrf': 0.060, 'dense': 0.0, 'bm25': 0.0},
    }

    # 旧版 0.055*0.7=0.0385、导语 0.060*0.8=0.048，均低于 a1 的 0.050
    ordered = ranking.order_units(
        ['a1', 'a2', 'a3', 'b1', 'old', 'head'], records, {'old'}, {'head': True})
    assert ordered[:2] == ['a1', 'head']
    assert 'old' not in ordered[:2]

    # 前三名同文档时，用最后一个名额换入另一文档的候选
    diverse = ranking.diversify_documents(['a1', 'a2', 'a3', 'b1'], 3, unit_map, records)
    assert diverse == ['a1', 'a2', 'b1']
    # 另一文档得分不足一半时保持原选择
    records['b1']['rrf'] = 0.001
    assert ranking.diversify_documents(['a1', 'a2', 'a3', 'b1'], 3, unit_map, records) \
        == ['a1', 'a2', 'a3']

    # 条款证据已并入表格行后，这些行不再单独占证据位
    table_rows = [{'id': 'row1'}, {'id': 'row2'}]
    chosen = ranking.fill_with_attached_rows(
        ['a1'], ['a1', 'row1', 'b1', 'row2'], 2, unit_map,
        lambda item: table_rows if item['id'] == 'a1' else [])
    assert chosen == ['a1', 'b1']


# ---------------------------------------------------------------------------
# 7. 证据：表格挂接、抬头、超长片段
# ---------------------------------------------------------------------------

def test_evidence_table_attachment_header_and_excerpt() -> None:
    article = unit('art', 'd-tab', '废弃物处置办法', chapter='第二章 处置流程',
                   article='第八条', article_no=8, kind='article', order=10,
                   text='第八条 实验废弃物分类处置标准如下：', pages=[5])
    header_row = unit('row-h', 'd-tab', '废弃物处置办法', kind='table_row', order=11,
                      table={'name': '废弃物分类表', 'headers': ['项目', '标准'],
                             'row': ['项目', '标准']})
    data_row = unit('row-1', 'd-tab', '废弃物处置办法', kind='table_row', order=12,
                    text='废液 | 交专业公司回收',
                    table={'name': '废弃物分类表', 'headers': ['项目', '标准'],
                           'row': ['废液', '交专业公司回收']}, pages=[5])
    doc_units = {'d-tab': [article, header_row, data_row]}

    article_evidence = evidence.build_evidence(
        'art', article, {'passage_id': 'p1'}, doc_units=doc_units, unit_passages={})
    assert article_evidence.startswith('《废弃物处置办法》 第二章 处置流程 第八条（第5页）')
    assert '表格：废弃物分类表' in article_evidence
    assert '行：废液 | 交专业公司回收' in article_evidence
    # 表头行不作为数据行重复输出
    assert '行：项目 | 标准' not in article_evidence

    row_evidence = evidence.build_evidence(
        'row-1', data_row, {'passage_id': 'p3'}, doc_units=doc_units, unit_passages={})
    assert '列：项目 | 标准' in row_evidence
    assert '行：废液 | 交专业公司回收' in row_evidence

    # 超长单元：取命中片段 ±1 段而不是整段
    big = unit('big', 'd-big', '长文本办法', text='长文本内容。' * 800)
    passages = {'big': [{'id': 'p1', 'text': '第一段'}, {'id': 'p2', 'text': '第二段'},
                        {'id': 'p3', 'text': '第三段'}]}
    big_evidence = evidence.build_evidence(
        'big', big, {'passage_id': 'p2'}, doc_units={}, unit_passages=passages)
    assert '第一段' in big_evidence and '第三段' in big_evidence
    assert '长文本内容。' * 800 not in big_evidence

    assert evidence.count_tokens('证据文本') == 4
    assert evidence.count_tokens('') == 1
    assert evidence.clip_tokens('一二三四五', 3) == '一二三'


# ---------------------------------------------------------------------------
# 8. 证据：预算提示、notes 合并、版本提示
# ---------------------------------------------------------------------------

def test_evidence_notes_budget_and_version_hint() -> None:
    assert ranking.note_over_budget('差旅费管理办法', 4096) \
        == '《差旅费管理办法》证据超出上下文预算（4096 tokens），已省略'

    # 版本提示在前、预算提示在后，且各自去重
    merged = ranking.merge_notes(['版本提示A', '版本提示A'], ['预算提示', ''])
    assert merged == ['版本提示A', '预算提示']

    assert evidence.wants_version_hint('旧版标准还有效吗？', '差旅住宿标准') is True
    assert evidence.wants_version_hint('差旅住宿标准是多少？', '差旅住宿标准') is False

    from app.domain import versions
    as_of = '2025-10-01'
    units = [unit('old', 'd-old', '差旅费管理办法', doc_no='校发〔2020〕5号',
                  status='superseded', effective_from='2020-01-01',
                  effective_to='2025-08-31', version_label='2020版'),
             unit('new', 'd-new', '差旅费管理办法（2025年修订）',
                  effective_from='2025-09-01', version_label='2025修订版')]
    current = versions.current_documents(units, as_of)
    note = evidence.version_note(units[0], current, {'d-new': units[1]})
    assert note and '旧版内容仅作参考' in note
    assert evidence.version_note(units[1], current, {'d-new': units[1]}) is None
