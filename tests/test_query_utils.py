"""查询工具的信息回归测试（Stage 1 迁移时曾因删除局部正则导致 doc_no_key 崩溃）。"""
from app.retrieval.query import doc_no_key, normalize, parse_query


def test_doc_no_key_handles_spaces_and_brackets():
    assert doc_no_key('国办发  〔2024〕 12 号') == '国办发〔2024〕12号'
    assert doc_no_key('') == ''


def test_normalize_collapses_whitespace():
    assert normalize('  差旅费\u3000 管理办法\n第二条 ') == '差旅费 管理办法 第二条'


def test_parse_query_recognizes_article_and_doc_no():
    parsed = parse_query('《差旅费管理办法》第八条规定的住宿标准是多少')
    assert parsed.get('article')
    parsed_doc = parse_query('国办发〔2024〕12号里怎么规定的')
    assert parsed_doc.get('doc_no')
