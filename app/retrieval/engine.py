"""混合检索编排：BGE 向量 + BM25 + 条款/文号/章节精确通道 → RRF → 过滤 → 排序 → 证据。

接口见 docs/架构与接口.md 3.11，返回结构见 2.5。检索结果只来自发布目录的
units/passages，不做任何改写，保证每条证据都能在 units.jsonl 中找到。

Stage 2 起本文件只保留编排与缓存，各阶段实现分在：
    channels.py  候选召回（dense / BM25 / 文号 / 条款号 / 章节 / 标题）
    fusion.py    单元级候选记录与加权 RRF
    filters.py   状态、时效、表头、章节导语、相关性门控
    ranking.py   降权排序、跨文档多样性、条数控制、notes 合并
    evidence.py  证据文本、表格挂接、token 预算、版本提示
"""
from __future__ import annotations

import json
import time
from datetime import date
from pathlib import Path
from typing import Callable

from app import config
from app.domain import versions as versioning
from app.retrieval import cache as cache_module
from app.retrieval import channels, fusion, planner, ranking
from app.retrieval import evidence as evidence_builder
from app.retrieval import filters as stage_filters
from app.retrieval.backends import create_backend
from app.retrieval.channels import DENSE_TOP, KEYWORD_TOP
from app.retrieval.evidence import EVIDENCE_MAX_TOKENS
from app.retrieval.fusion import EXACT_CHANNELS, RRF_K
from app.retrieval.lexical import PostingBM25, words
from app.retrieval.query import (
    doc_no_key,
    normalize,
    parse_article,
    parse_query,
)

__all__ = [
    'PolicyRetriever', 'DENSE_TOP', 'KEYWORD_TOP', 'RRF_K',
    'EVIDENCE_MAX_TOKENS', 'EXACT_CHANNELS',
]


def _apply_rerank(question: str, ordered: list[str],
                  unit_map: dict[str, dict],
                  records: dict[str, dict] | None = None) -> list[str]:
    """用 bge-reranker 对前 RERANK_TOP_N 个候选重排；失败时回退原顺序。"""
    limit = int(getattr(config, 'RERANK_TOP_N', 0) or 0)
    if limit <= 0 or len(ordered) <= 1:
        return ordered
    from app.retrieval.rerank import get_reranker

    reranker = get_reranker()
    if reranker is None:
        return ordered
    head, tail = ordered[:limit], ordered[limit:]
    texts = []
    for unit_id in head:
        unit = unit_map.get(unit_id) or {}
        parts = [str(unit.get(key) or '') for key in ('title', 'article', 'text')]
        texts.append(' '.join(part for part in parts if part).strip())
    try:
        scores = reranker.score(question, texts)
    except Exception as exc:  # 单次失败不影响问答，下一次调用仍会重试
        logger = __import__('logging').getLogger(__name__)
        logger.warning('重排失败，已回退原排序：%s', exc)
        return ordered
    if len(scores) != len(head):
        return ordered
    if records is not None:
        for unit_id, score in zip(head, scores):
            record = records.get(unit_id)
            if record is not None:
                record.setdefault('scores', {})['rerank'] = round(float(score), 4)
    order = sorted(range(len(head)), key=lambda index: (-scores[index], index))
    return [head[index] for index in order] + tail


def _read_jsonl(path: Path) -> list[dict]:
    if not path.is_file():
        return []
    rows: list[dict] = []
    with path.open('r', encoding='utf-8') as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(item, dict):
                rows.append(item)
    return rows


class PolicyRetriever:
    """发布目录上的混合检索器。"""

    def __init__(self, release_dir: Path, backend: str = 'chroma',
                 device: str | None = None,
                 catalog_path: Path | None = None,
                 encoder=None) -> None:
        self.release_dir = Path(release_dir)
        self.backend_name = str(backend or config.VECTOR_BACKEND or 'chroma').lower()
        self.device = str(device or config.resolve_device())
        self.catalog_path = Path(catalog_path) if catalog_path else None
        # 允许测试注入假编码器，避免单测加载真实模型
        self._encoder = encoder
        self.backend_note: str | None = None
        self.cache = cache_module.ResultCache(int(getattr(config, 'CACHE_SIZE', 256) or 256))

        self.manifest = self._load_manifest()
        self.release_id = str((self.manifest or {}).get('release_id') or self.release_dir.name)
        units_path = self.release_dir / 'units.jsonl'
        if not units_path.is_file():
            raise RuntimeError(f'发布目录缺少 units.jsonl：{units_path}')

        self.units = _read_jsonl(units_path)
        self.passages = _read_jsonl(self.release_dir / 'passages.jsonl')
        self.unit_map: dict[str, dict] = {
            str(unit['id']): unit for unit in self.units if unit.get('id')
        }
        self.passage_map: dict[str, dict] = {
            str(passage['id']): passage for passage in self.passages if passage.get('id')
        }

        # unit -> 按位置排序的 passages，用于长单元取「命中片段 ±1 段」
        self.unit_passages: dict[str, list[dict]] = {}
        for passage in self.passages:
            unit_id = str(passage.get('unit_id') or '')
            if unit_id in self.unit_map:
                self.unit_passages.setdefault(unit_id, []).append(passage)
        for items in self.unit_passages.values():
            items.sort(key=lambda item: (int(item.get('start') or 0), str(item.get('id'))))

        # 文档 -> 单元样本，用于版本/时效判断
        self._doc_units: dict[str, list[dict]] = {}
        for unit in self.units:
            self._doc_units.setdefault(str(unit.get('doc_id') or ''), []).append(unit)
        # 按文档原始顺序排列，便于把紧跟在条款后的表格并进条款证据
        for items in self._doc_units.values():
            items.sort(key=lambda item: (int(item.get('order') or 0), str(item.get('id') or '')))
        self._doc_samples: dict[str, dict] = {
            doc_id: items[0] for doc_id, items in self._doc_units.items() if items
        }

        # BM25 索引建在 passage 文本上
        self._passage_ids = [str(passage.get('id')) for passage in self.passages]
        token_lists = [words(str(passage.get('text') or '')) for passage in self.passages]
        self.bm25 = PostingBM25(self._passage_ids, token_lists)

        self.collection = self._collection_name()
        self._backend = self._open_backend()
        # 同一基准日的现行版本映射可复用，避免每次检索都全量扫描 units
        self._current_cache: dict[str, dict[str, str]] = {}

    # ------------------------------------------------------------------
    # 初始化辅助
    # ------------------------------------------------------------------
    def _load_manifest(self) -> dict:
        path = self.release_dir / 'manifest.json'
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            return {}
        return value if isinstance(value, dict) else {}

    def _collection_name(self) -> str:
        name = self.manifest.get('collection')
        if name:
            return str(name)
        from app.store.releases import collection_name
        release_id = str(self.manifest.get('release_id') or self.release_dir.name)
        return collection_name(release_id)

    def _open_backend(self):
        wanted = self.backend_name
        npz_available = (self.release_dir / 'vectors.npz').is_file()
        try:
            backend = create_backend(wanted, self.release_dir, self.collection)
            if wanted == 'chroma' and backend.count() == 0 and npz_available:
                self.backend_name = 'npz'
                self.backend_note = 'Chroma 集合为空，已回退 npz 后端'
                return create_backend('npz', self.release_dir, self.collection)
            return backend
        except Exception as exc:
            if wanted != 'npz' and npz_available:
                self.backend_name = 'npz'
                self.backend_note = f'{wanted} 后端不可用（{exc}），已回退 npz 后端'
                return create_backend('npz', self.release_dir, self.collection)
            raise RuntimeError(f'向量后端初始化失败：{exc}') from exc

    @property
    def encoder(self):
        """真实编码器懒加载：只做 BM25 检索或单测时不需要加载模型。"""
        if self._encoder is None:
            from app.retrieval.encoder import BgeEncoder
            self._encoder = BgeEncoder(device=self.device)
            self.device = self._encoder.device
        return self._encoder

    # ------------------------------------------------------------------
    # 信息
    # ------------------------------------------------------------------
    def info(self) -> dict:
        embedding = self.manifest.get('embedding') or {}
        return {
            'cache': self.cache.stats(),
            'release_id': self.manifest.get('release_id') or self.release_dir.name,
            'release_dir': str(self.release_dir),
            'backend': self.backend_name,
            'backend_note': self.backend_note,
            'collection': self.collection,
            'device': self.device,
            'embedding_model': embedding.get('model') or config.EMBEDDING_DIR.name,
            'dimension': embedding.get('dimension') or getattr(self._backend, 'dimension', None),
            'doc_count': len(self._doc_units),
            'unit_count': len(self.units),
            'passage_count': len(self.passages),
        }

    # ------------------------------------------------------------------
    # 主检索（编排）
    # ------------------------------------------------------------------
    def search(self, question: str, top_k: int = 5,
               filters: dict | None = None,
               on_stage: Callable | None = None) -> dict:
        started = time.perf_counter()
        filters = dict(filters or {})
        top_k = max(1, int(top_k or config.TOP_K or 5))
        as_of = str(filters.get('as_of') or date.today().isoformat())
        department_filter = normalize(str(filters.get('department') or '')) or None
        doc_filter = normalize(str(filters.get('doc_id') or '')) or None

        # 支持服务层传入改写后的问题（多轮补全），检索用它，返回值仍保留原问题
        base = normalize(str(filters.get('rewritten') or filters.get('query') or question or ''))
        parsed = parse_query(base or question or '')
        if filters.get('doc_no'):
            parsed['doc_no'] = normalize(str(filters['doc_no']))
        if filters.get('article'):
            article, article_no = parse_article(str(filters['article']))
            parsed['article'] = article
            parsed['article_no'] = article_no
        rewritten = parsed.get('text') or base or normalize(question)
        notes: list[str] = []
        if self.backend_note:
            notes.append(self.backend_note)

        # ---- 查询拆解：一句多问时按子句分别召回，再交错合并 ----
        known_titles = [str(unit.get('doc_title') or '') for unit in self._doc_samples.values()]
        # 拆解以「用户原话」为准：改写常把「分别/同时」抹平，单话题则沿用改写问句
        plan = planner.plan_query(normalize(question) or rewritten, titles=known_titles)
        sub_queries = list(plan.sub_queries) if plan.is_multi else [rewritten]
        if plan.is_multi:
            notes.append(f'问题包含 {len(sub_queries)} 个子问题，已分别检索后合并证据')

        self._emit(on_stage, 'query', {
            'question': question,
            'rewritten': rewritten,
            'sub_queries': sub_queries if plan.is_multi else [],
            'filters': {'department': department_filter, 'as_of': as_of},
            'doc_no': parsed.get('doc_no'),
            'article': parsed.get('article'),
            'is_followup': parsed.get('is_followup'),
        })

        # ---- 结果缓存：同问、同过滤、同 release、同基准日直接复用（T9） ----
        # 改写问句不进缓存键：同一问题在不同轮次被改写成不同措辞时，按同一问题复用证据。
        cache_filters = {name: value for name, value in (filters or {}).items() if name != 'rewritten'}
        cache_key = cache_module.make_key(self.release_id, question, cache_filters, as_of, top_k)
        cached = self.cache.get(cache_key)
        if cached is not None:
            cached['seconds'] = round(time.perf_counter() - started, 3)
            cached['cached'] = True
            self._emit(on_stage, 'evidence', {
                'titles': [evidence_builder.unit_label(item['unit']) for item in cached.get('results', [])],
                'notes': list(cached.get('notes') or []),
            })
            return cached

        # ---- 通道 1~6：每个子句跑一遍向量 / BM25 / 条款号 / 文号 / 章节 / 标题 ----
        dense_groups: list[list[tuple[str, float]]] = []
        keyword_groups: list[list[tuple[str, float]]] = []
        exact_groups: dict[str, list[list[str]]] = {
            name: [] for name in fusion.EXACT_CHANNELS}
        for index, sub_query in enumerate(sub_queries):
            if index == 0:
                sub_parsed = parsed
            else:
                sub_parsed = parse_query(sub_query)
                if filters.get('doc_no'):
                    sub_parsed['doc_no'] = parsed['doc_no']
                if filters.get('article'):
                    sub_parsed['article'] = parsed.get('article')
                    sub_parsed['article_no'] = parsed.get('article_no')

            hits, dense_note = channels.dense_candidates(
                self.encoder, self._backend, sub_query, doc_filter,
            )
            if dense_note:
                notes.append(dense_note)
            dense_groups.append(hits)

            keyword_hits, keyword_note = channels.keyword_candidates(
                self.bm25, self._passage_ids, sub_query,
            )
            if keyword_note:
                notes.append(keyword_note)
            keyword_groups.append(keyword_hits)

            found = channels.exact_channels(self.units, self.unit_map, sub_parsed)
            for name in fusion.EXACT_CHANNELS:
                exact_groups[name].append(found.get(name) or [])

        dense_hits = fusion.merge_hits(dense_groups)
        keyword_hits = fusion.merge_hits(keyword_groups)
        exact_channels = {
            name: fusion.interleave(groups)
            for name, groups in exact_groups.items() if any(groups)
        }

        # ---- 每个子句各自融合，再按单元累加 RRF（多子问题 = 多路召回求和）----
        records: dict[str, dict] = {}
        sub_ranked: list[list[str]] = []
        for index in range(len(sub_queries)):
            sub_records: dict[str, dict] = {}
            dense_units = fusion.add_dense_candidates(
                sub_records, dense_groups[index], self.passage_map, self.unit_map)
            keyword_units = fusion.add_keyword_candidates(
                sub_records, keyword_groups[index], self.passage_map, self.unit_map)
            channel_lists: dict[str, list[str]] = {
                'BGE': dense_units,
                'BM25': keyword_units,
            }
            for name in fusion.EXACT_CHANNELS:
                hits = exact_groups[name][index]
                if hits:
                    channel_lists[name] = hits
            fusion.fuse(sub_records, channel_lists, self.unit_map, self.unit_passages)
            fusion.merge_into(records, sub_records)
            sub_ranked.append([
                unit_id for unit_id, _ in sorted(
                    sub_records.items(), key=lambda item: -float(item[1].get('rrf') or 0.0))
            ])

        dense_ids = [pid for pid, _ in dense_hits]
        self._emit(on_stage, 'vectors', {
            'passage_count': len(dense_hits),
            'titles': evidence_builder.labels_for_passages(
                dense_ids, self.passage_map, self.unit_map, 5),
        })
        self._emit(on_stage, 'keywords', {
            'passage_count': len(keyword_hits),
            'titles': evidence_builder.labels_for_passages(
                [pid for pid, _ in keyword_hits], self.passage_map, self.unit_map, 5),
        })

        # ---- 相关性闸门：问题与知识库完全无关时，不要拿低分结果硬凑证据 ----
        min_dense = float(getattr(config, 'RETRIEVAL_MIN_DENSE', 0.45) or 0.0)
        gate = stage_filters.relevance_gate(dense_hits, exact_channels, min_dense)
        if gate.blocked:
            notes = ranking.dedupe(notes + [stage_filters.GATE_REFUSAL_NOTE])
            self._emit(on_stage, 'evidence', {'titles': [], 'notes': notes})
            return {
                'question': question,
                'rewritten': rewritten,
                'filters': {'department': department_filter, 'as_of': as_of},
                'results': [],
                'context': '',
                'tokens': 0,
                'notes': notes,
                'seconds': round(time.perf_counter() - started, 3),
            }

        # ---- 时效 / 版本 / 硬过滤 ----
        current_docs = self._current_docs(as_of)
        explicit_doc_key = doc_no_key(parsed.get('doc_no') or '') if parsed.get('doc_no') else None
        filtered = stage_filters.apply_hard_filters(
            records, self.unit_map, current_docs, as_of,
            doc_filter=doc_filter,
            department_filter=department_filter,
            explicit_doc_key=explicit_doc_key,
        )
        usable = filtered.usable
        stale_ids = filtered.stale_ids
        stale_candidates = filtered.stale_candidates

        # ---- 章节导语去重与排序 ----
        heading_flags = stage_filters.heading_flags(usable, self.unit_map)
        kept = stage_filters.dedupe_heading_units(usable, heading_flags, self.unit_map)
        ordered = ranking.order_units(kept, records, stale_ids, heading_flags)
        # 交叉编码器重排：只影响前若干名，模型不可用时原样返回
        ordered = _apply_rerank(rewritten or base, ordered, self.unit_map, records)
        # 问题含数值/期限时，含该数值的条款/表格行优先；同一 article 的碎片合并成一个证据位
        prefer_tokens = evidence_builder.numeric_tokens(normalize(question), rewritten)
        ordered = ranking.prefer_numeric_units(ordered, prefer_tokens, self.unit_map)
        ordered, merged_article_units = ranking.merge_article_units(ordered, self.unit_map)

        # 跨制度问题：前几名全来自同一份文档时，用最后一个名额换入另一份文档
        selection = ranking.diversify_documents(ordered, top_k, self.unit_map, records)
        if len(sub_queries) > 1:
            usable_set = set(kept)
            quotas = [[uid for uid in group if uid in usable_set][:5] for group in sub_ranked]
            selection = ranking.enforce_quotas(selection, ordered, quotas, top_k)
        # 条款证据会带上紧邻的表格行；这些行不再单独占一个证据位
        selection = ranking.fill_with_attached_rows(
            selection, ordered, top_k, self.unit_map,
            lambda unit: evidence_builder.adjacent_table_rows(unit, self._doc_units),
        )

        # ---- 组装证据与 token 预算 ----
        budget = int(getattr(config, 'MAX_CONTEXT_TOKENS', 4096) or 4096)
        tokenizer = getattr(self._encoder, 'tokenizer', None) if self._encoder else None
        results: list[dict] = []
        used_tokens = 0
        version_notes: list[str] = []
        for unit_id in selection:
            if len(results) >= top_k:
                break
            record = records[unit_id]
            unit = self.unit_map[unit_id]
            # 单条结果也要过相关性：向量分太低、关键词也不强、又不是精确命中时，
            # 不放进证据，避免回答里出现「奖学金办法」这类无关引用。
            if not stage_filters.passes_result_gate(record, min_dense, gate.max_dense):
                continue
            merged_units = merged_article_units.get(unit_id) or []
            evidence = evidence_builder.build_evidence(
                unit_id, unit, record,
                doc_units=self._doc_units,
                unit_passages=self.unit_passages,
                tokenizer=tokenizer,
                prefer=prefer_tokens,
                merged_units=merged_units,
            )
            cost = evidence_builder.count_tokens(evidence, tokenizer)
            if used_tokens + cost > budget:
                notes.append(ranking.note_over_budget(str(unit.get('doc_title') or ''), budget))
                continue
            used_tokens += cost
            if unit_id in stale_ids:
                note = evidence_builder.version_note(unit, current_docs, self._doc_samples)
                if note:
                    version_notes.append(note)
            scores = {
                'dense': round(float(record['dense']), 4),
                'bm25': round(float(record['bm25']), 3),
                'rrf': round(float(record['rrf']), 6),
            }
            if 'rerank' in (record.get('scores') or {}):
                scores['rerank'] = record['scores']['rerank']
            citation = evidence_builder.citation(unit)
            if merged_units:
                citation['pages'] = sorted(set(citation.get('pages') or [])
                                           | {page for other in merged_units
                                              for page in evidence_builder.pages(other)})
            results.append({
                'rank': len(results) + 1,
                'unit': dict(unit),
                'evidence': evidence,
                'citation': citation,
                'merged_units': [str(other.get('id')) for other in merged_units],
                'channels': [name for name in fusion.CHANNEL_ORDER if name in record['channels']],
                'scores': scores,
            })

        # 用户问到的主题若存在更新版本，即便旧版没被选进证据也要提示；
        # 只有「问题确实在问版本/有效性」且旧版候选与最佳证据得分接近时才提示。
        best_selected = ranking.best_selected_score(results, records)
        if evidence_builder.wants_version_hint(question, rewritten) or explicit_doc_key:
            for unit_id, score in sorted(stale_candidates, key=lambda pair: -pair[1]):
                if not ranking.version_note_score_ok(score, best_selected):
                    continue
                note = evidence_builder.version_note(
                    self.unit_map[unit_id], current_docs, self._doc_samples)
                if note:
                    version_notes.append(note)

        # 版本提示放在预算提示之前，更符合阅读顺序
        notes = ranking.merge_notes(version_notes, notes)
        context = '\n\n'.join(
            f'[证据{item["rank"]}] {item["evidence"]}' for item in results
        )
        seconds = round(time.perf_counter() - started, 3)
        self._emit(on_stage, 'evidence', {
            'titles': [evidence_builder.unit_label(item['unit']) for item in results],
            'notes': notes,
        })

        payload = {
            'question': question,
            'rewritten': rewritten,
            'filters': {'department': department_filter, 'as_of': as_of},
            'results': results,
            'context': context,
            'tokens': used_tokens,
            'notes': notes,
        }
        self.cache.put(cache_key, payload)
        return {**payload, 'seconds': seconds, 'cached': False}

    # ------------------------------------------------------------------
    # 时效与版本（规则在 app/domain/versions.py，这里只做适配与缓存）
    # ------------------------------------------------------------------
    def _current_docs(self, as_of: str) -> dict[str, str]:
        cached = self._current_cache.get(as_of)
        if cached is None:
            cached = versioning.current_documents(self.units, as_of)
            self._current_cache[as_of] = cached
        return cached

    # ------------------------------------------------------------------
    # 事件推送
    # ------------------------------------------------------------------
    @staticmethod
    def _emit(on_stage: Callable | None, stage: str, payload: dict) -> None:
        if not on_stage:
            return
        try:
            on_stage(stage, payload)
        except Exception:
            # 推送失败不应影响检索本身
            pass
