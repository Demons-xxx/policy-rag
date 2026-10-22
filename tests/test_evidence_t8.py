"""T8 证据组装：数值/期限优先、同条款碎片合并、表格行对齐。"""
from __future__ import annotations

from app.retrieval import evidence, ranking


def _unit(uid: str, text: str, *, kind: str = 'article', article: str = '第十条',
          chapter: str = '第二章', doc_id: str = 'doc-1', table: dict | None = None) -> dict:
    return {
        'id': uid, 'kind': kind, 'text': text, 'article': article,
        'chapter': chapter, 'section': '', 'doc_id': doc_id, 'order': 1,
        'table': table or {},
    }


def test_numeric_tokens_extracts_limits():
    tokens = evidence.numeric_tokens('差旅住宿费超过 650 元怎么报？3 个工作日内提交，比例 80％')
    assert '650元' in tokens
    assert '3个工作日' in tokens
    assert '80％' in tokens
    # 单个数字不算数值证据，避免把「第五条」当期限
    assert evidence.numeric_tokens('第五条怎么说') == []


def test_table_block_puts_matching_row_first_and_skips_header_row():
    headers = ['项目', '标准']
    rows = [
        _unit('r0', '', kind='table_row', article='', chapter='第二章',
              table={'name': '住宿标准', 'headers': headers, 'row': headers}),
        _unit('r1', '', kind='table_row', article='', chapter='第二章',
              table={'name': '住宿标准', 'headers': headers, 'row': ['市内交通', '50元/天']}),
        _unit('r2', '', kind='table_row', article='', chapter='第二章',
              table={'name': '住宿标准', 'headers': headers, 'row': ['住宿费', '650元/人·天']}),
    ]
    block = evidence.table_block(rows, prefer=['650元'])
    lines = block.splitlines()
    assert lines[0] == '表格：住宿标准'
    assert lines[1] == '列：项目 | 标准'
    assert lines[2] == '行：住宿费 | 650元/人·天'
    assert '行：项目 | 标准' not in block


def test_prefer_numeric_units_moves_hit_to_front_of_window():
    unit_map = {
        'a': _unit('a', '报销流程需要先审批。'),
        'b': _unit('b', '材料须在 3 个工作日内提交。'),
        'c': _unit('c', '其它条款。'),
    }
    ordered = ['a', 'b', 'c']
    assert ranking.prefer_numeric_units(ordered, ['3个工作日'], unit_map) == ['b', 'a', 'c']
    assert ranking.prefer_numeric_units(ordered, [], unit_map) == ordered


def test_merge_article_units_keeps_first_and_collects_followups():
    unit_map = {
        'u1': _unit('u1', '第一条 总则……'),
        'u2': _unit('u2', '第一条 总则的补充说明。'),
        'u3': _unit('u3', '第二条 适用范围。', article='第十一条'),
        's1': _unit('s1', '导语', kind='section', article=''),
    }
    kept, merged = ranking.merge_article_units(['u1', 'u2', 'u3', 's1'], unit_map)
    assert kept == ['u1', 'u3', 's1']
    assert [u['id'] for u in merged['u1']] == ['u2']


def test_build_evidence_appends_merged_units_after_main_text():
    main = _unit('u1', '第一条 总则：报销须经审批。')
    other = _unit('u2', '第一条 补充：单笔超过 5000 元须院领导签字。')
    text = evidence.build_evidence(
        'u1', main, {'passage_id': None}, {}, {}, tokenizer=None,
        prefer=['5000元'], merged_units=[other])
    assert '总则：报销须经审批' in text
    assert '单笔超过 5000 元须院领导签字' in text
    assert text.index('总则') < text.index('5000 元')
