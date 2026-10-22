"""领域包 policy 的时效规则单测（纯函数，不碰模型与网络）。"""
from app.domain import versions


def unit(doc_id='d1', title='差旅费管理办法', status='active',
         start='2025-01-01', end='', version=''):
    return {
        'doc_id': doc_id, 'doc_title': title, 'status': status,
        'effective_from': start, 'effective_to': end, 'version_label': version,
    }


def test_title_key_strips_version_suffix():
    assert versions.title_key(unit(title='差旅费管理办法（2025年修订）')) == '差旅费管理办法'
    assert versions.title_key(unit(title='《差旅费管理办法》')) == '差旅费管理办法'


def test_in_window_and_expired():
    assert versions.in_window(unit(start='2025-01-01'), '2025-06-01')
    assert not versions.in_window(unit(start='2026-01-01'), '2025-06-01')
    assert versions.expired(unit(end='2025-01-01'), '2025-06-01')
    assert not versions.expired(unit(end=''), '2025-06-01')


def test_current_documents_prefers_active_in_window():
    units = [
        unit('d-old', title='差旅费管理办法', status='superseded', start='2020-01-01'),
        unit('d-new', title='差旅费管理办法', status='active', start='2025-01-01'),
    ]
    assert versions.current_documents(units, '2025-06-01')['差旅费管理办法'] == 'd-new'


def test_current_documents_ignores_future_and_repealed():
    units = [
        unit('d-now', title='采购管理办法', status='active', start='2024-01-01'),
        unit('d-future', title='采购管理办法', status='active', start='2026-01-01'),
        unit('d-dead', title='采购管理办法', status='repealed', start='2023-01-01'),
    ]
    assert versions.current_documents(units, '2025-06-01')['采购管理办法'] == 'd-now'


def test_version_note_marks_old_version():
    units = [unit('d-new', status='active', start='2025-01-01', version='2025年版')]
    current = versions.current_documents(units, '2025-06-01')
    note = versions.version_note(unit('d-old', version='2020年版'), current, {})
    assert '现行版本' in note or '自2025-01-01起施行' in note


# --------------------------------------------------------------- 版本链推断（T10）

def _doc(doc_id, title, doc_no='', effective_from='2024-01-01', status='active'):
    return {'doc_id': doc_id, 'title': title, 'doc_no': doc_no,
            'effective_from': effective_from, 'status': status}


def test_infer_version_chains_same_doc_no_newer_wins():
    from app.domain.versions import infer_version_chains

    docs = [
        _doc('old', 'XX大学科研经费报销实施细则', '校发〔2024〕38号', '2024-05-01'),
        _doc('new', 'XX大学科研经费报销实施细则', '校发〔2024〕38号', '2026-01-01'),
    ]
    relations = infer_version_chains(docs)
    assert relations == [{'old_doc_id': 'old', 'new_doc_id': 'new', 'reason': '同文号 校发〔2024〕38号'}]


def test_infer_version_chains_title_similarity_threshold():
    from app.domain.versions import infer_version_chains

    docs = [
        _doc('a', '实验室安全管理规定', '', '2024-01-01'),
        _doc('b', '实验室安全管理规定', '', '2025-06-01'),
        _doc('c', '完全不同的会议管理办法', '', '2026-01-01'),
    ]
    relations = infer_version_chains(docs, threshold=0.9)
    assert {(r['old_doc_id'], r['new_doc_id']) for r in relations} == {('a', 'b')}
    assert relations[0]['reason'].startswith('标题相似度')


def test_infer_version_chains_ignores_non_newer_and_missing_dates():
    from app.domain.versions import infer_version_chains

    docs = [
        _doc('a', '同一办法', '文号X', '2025-01-01'),
        _doc('b', '同一办法', '文号X', '2025-01-01'),  # 同一天，不算新版
        _doc('c', '另一办法', '文号Y', ''),             # 无生效日期，不参与
        _doc('d', '另一办法', '文号Y', '2026-01-01'),
    ]
    assert infer_version_chains(docs) == []


def test_infer_version_chains_skips_repealed_docs():
    from app.domain.versions import infer_version_chains

    docs = [
        _doc('a', '同一办法', '文号X', '2024-01-01', status='repealed'),
        _doc('b', '同一办法', '文号X', '2025-01-01'),
    ]
    assert infer_version_chains(docs) == []
