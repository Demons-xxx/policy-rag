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
