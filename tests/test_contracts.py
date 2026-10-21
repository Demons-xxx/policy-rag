"""契约测试：真实实现必须满足 app/core/contracts.py 的 Protocol。

这些断言很轻（只检查方法是否存在、能否调用），作用是：
重构时如果改了某部件的对外方法名/参数，这里立刻报错。
"""
import numpy as np

from app.core import contracts
from app.core.contracts import Citation, RetrievalOutcome
from app.retrieval.lexical import PostingBM25
from app.store.catalog import Catalog
from app.store import releases


def test_citation_to_dict_roundtrip():
    cite = Citation(unit_id='u1', doc_id='d1', doc_title='制度', article_no='第一条',
                    version_label='', page_from=3, note='旧版')
    data = cite.to_dict()
    assert data['unit_id'] == 'u1' and data['page'] == 3
    assert data['note'] == '旧版'
    assert 'page' not in Citation(unit_id='u', doc_id='d', page_from=None).to_dict()


def test_retrieval_outcome_defaults():
    outcome = RetrievalOutcome()
    assert outcome.results == [] and outcome.context == '' and outcome.notes == []
    assert outcome.max_dense == 0.0


def test_lexical_index_satisfies_contract():
    index = PostingBM25(['p1', 'p2'], [['差旅', '费'], ['采购', '流程']])
    assert isinstance(index, contracts.LexicalIndex)
    scores = index.get_scores(['差旅'])
    assert len(scores) == 2


def test_catalog_satisfies_contract(tmp_path):
    with Catalog(tmp_path / 'catalog.sqlite') as catalog:
        assert isinstance(catalog, contracts.CatalogStore)
        catalog.upsert_document({'doc_id': 'd1', 'title': '测试制度', 'sha256': 'x',
                                 'source_path': 'a.pdf', 'status': 'active'})
        assert catalog.get_document('d1')['title'] == '测试制度'


def test_release_store_module_satisfies_contract():
    class ReleaseFacade:
        read_active = staticmethod(releases.read_active)
        write_active = staticmethod(releases.write_active)
        list_releases = staticmethod(releases.list_releases)

    assert isinstance(ReleaseFacade(), contracts.ReleaseStore)


def test_encoder_and_client_shape():
    class FakeEncoder:
        dimension = 3

        def encode(self, texts, query=False, batch_size=32):
            return np.zeros((len(texts), 3), dtype=np.float32)

        def count_tokens(self, text):
            return len(text)

    class FakeClient:
        def stream(self, messages, max_tokens=1024, temperature=0.2):
            yield '答'

        def complete(self, messages, max_tokens=512, temperature=0.1):
            return '答'

    assert isinstance(FakeEncoder(), contracts.Encoder)
    assert isinstance(FakeClient(), contracts.LLMClient)
    assert isinstance(FakeEncoder(), contracts.Encoder) is True
