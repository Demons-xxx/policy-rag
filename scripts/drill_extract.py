"""抽包演练（Stage 7）：把「可复用内核」复制到空目录，用假模型跑通一次问答。

做的事：
1. 按 ``docs/架构.md`` §6 的清单，把内核模块复制到 ``data/tmp/drill-<时间>/app``；
2. 生成一个独立脚本 ``run_drill.py``：自己造一个小发布（units/passages/vectors/manifest），
   用假编码器 + 假生成模型跑「检索 → 证据 → 问答事件流」；
3. 子进程执行该脚本，确认返回值与事件序列。

演练目录里没有知识库样例、评测题库、真实模型、Chroma，也没有领域提示词以外的
制度内容——因此它证明了「这套架构可以被单独拿走」。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DRILL_ROOT = ROOT / 'data' / 'tmp' / f'drill-{time.strftime("%Y%m%d-%H%M%S")}'

#: 抽取清单：只带通用内核 + 一个最小领域包
COPY_FILES = [
    '__init__.py', 'settings.py', 'config.py', 'project.json',
    'core/__init__.py', 'core/contracts.py', 'core/text.py',
    'domain/__init__.py', 'domain/versions.py',
    'retrieval/__init__.py', 'retrieval/encoder.py', 'retrieval/lexical.py',
    'retrieval/query.py', 'retrieval/engine.py', 'retrieval/channels.py',
    'retrieval/fusion.py', 'retrieval/filters.py', 'retrieval/ranking.py', 'retrieval/planner.py',
    'retrieval/evidence.py',
    'retrieval/backends/__init__.py', 'retrieval/backends/npz.py',
    'retrieval/backends/chroma.py',
    'llm/__init__.py', 'llm/client.py', 'llm/routing.py', 'llm/prompts.py',
    'qa/__init__.py', 'qa/flow.py',
]

RUN_DRILL = r'''"""演练脚本：半页代码造一个知识库，跑检索与问答（假编码器 + 假模型）。"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8')

from app.retrieval.engine import PolicyRetriever
from app.qa.flow import AnswerFlow, AnswerRequest

DIM = 64


def embed(text: str) -> list[float]:
    """字符哈希向量：字面越像、余弦越近，足以驱动检索链路。"""
    vector = np.zeros(DIM, dtype=np.float32)
    for char in str(text):
        vector[ord(char) % DIM] += 1.0
    norm = float(np.linalg.norm(vector))
    return (vector / norm if norm else vector).tolist()


UNITS = [
    ('d-travel', '差旅费管理办法', '第五条', '住宿费标准为每人每天不超过650元，凭住宿费发票报销。'),
    ('d-travel', '差旅费管理办法', '第八条', '市内交通费按实际发生额报销，单次不超过100元。'),
    ('d-lab', '实验室安全管理办法', '第七条', '危险化学品实行统一采购、集中储存、分级管理。'),
    ('d-leave', '教职工请假规定', '第三条', '请假3天以上的，由所在单位负责人审批后报人事处备案。'),
]


class FakeEncoder:
    dimension = DIM

    def encode(self, texts, query=False, batch_size=32):
        return np.asarray([embed(text) for text in texts], dtype=np.float32)

    def count_tokens(self, text):
        return len(str(text))


class FakeLLM:
    last_error = ''

    def ready(self):
        return True

    def stream(self, messages, max_tokens=1024, temperature=0.2):
        yield '根据《差旅费管理办法》第五条，住宿费标准为每人每天不超过650元。'

    def complete(self, messages, max_tokens=512, temperature=0.1):
        return '{"mode": "policy", "search_query": "住宿费标准", "reason": "制度问题"}'


def build_release(release_dir: Path) -> None:
    release_dir.mkdir(parents=True, exist_ok=True)
    units, passages, vectors = [], [], []
    for index, (doc_id, title, article, text) in enumerate(UNITS):
        unit_id = f'{doc_id}#u{index:02d}'
        passage_id = f'{doc_id}#p{index:02d}'
        units.append({
            'id': unit_id, 'doc_id': doc_id, 'doc_title': title, 'doc_no': '校发〔2025〕1号',
            'version_label': '2025年版', 'effective_from': '2025-01-01', 'effective_to': None,
            'department': '办公室', 'status': 'active', 'chapter': '第一章 总则',
            'section': None, 'article': article, 'article_no': index + 1, 'kind': 'article',
            'heading_path': ['第一章 总则', article], 'text': f'{article}  {text}',
            'page_from': 1, 'page_to': 1, 'order': index,
        })
        passages.append({
            'id': passage_id, 'unit_id': unit_id, 'doc_id': doc_id,
            'text': f'{article}  {text}', 'position': 0, 'token_count': len(text),
        })
        vectors.append(embed(text))
    for name, rows in (('units.jsonl', units), ('passages.jsonl', passages)):
        with (release_dir / name).open('w', encoding='utf-8') as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
    np.savez(
        release_dir / 'vectors.npz',
        ids=np.asarray([item['id'] for item in passages]),
        vectors=np.asarray(vectors, dtype=np.float32),
    )
    (release_dir / 'manifest.json').write_text(json.dumps({
        'release_id': release_dir.name, 'collection': 'policy_drill', 'format_version': 1,
        'doc_count': 3, 'unit_count': len(units), 'passage_count': len(passages),
        'embedding': {'model': 'fake-char-hash', 'dim': DIM},
    }, ensure_ascii=False), encoding='utf-8')


def main() -> int:
    release_dir = ROOT / 'release'
    build_release(release_dir)
    retriever = PolicyRetriever(release_dir, backend='npz', encoder=FakeEncoder())

    found = retriever.search('住宿费每晚上限是多少？', top_k=3)
    top = found['results'][0]['unit']
    assert found['results'], '检索没有结果'
    assert top['doc_title'] == '差旅费管理办法', top
    assert '650' in found['context'], found['context']

    events = list(AnswerFlow(
        llm_getter=lambda: FakeLLM(),
        retriever_getter=lambda: retriever,
        release_id_getter=lambda: 'rel-drill',
    ).run(AnswerRequest(question='住宿费标准是多少？')))
    stages = [event.stage for event in events]
    sources = next(event.payload.get('items') for event in events if event.stage == 'sources')
    done = next(event.payload for event in events if event.stage == 'done')

    assert stages[0] == 'routing', stages
    assert 'evidence' in stages and 'prompt' in stages, stages
    assert stages[-2:] == ['sources', 'done'], stages
    assert sources and sources[0]['title'] == '差旅费管理办法', sources
    assert isinstance(done.get('seconds'), float), done

    print('检索命中：', top['doc_title'], top['article'], '| 证据长度', len(found['context']))
    print('事件序列：', ' → '.join(stages))
    print('引用条数：', len(sources), '| 耗时：', done.get('seconds'))
    print('DRILL OK')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
'''


def copy_core(target: Path) -> list[str]:
    copied: list[str] = []
    for relative in COPY_FILES:
        source = ROOT / 'app' / relative
        if not source.is_file():
            raise FileNotFoundError(f'抽取清单里的文件不存在：{source}')
        destination = target / 'app' / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(relative)
    return copied


def run_drill() -> int:
    copied = copy_core(DRILL_ROOT)
    script = DRILL_ROOT / 'run_drill.py'
    script.write_text(RUN_DRILL, encoding='utf-8')
    environment = dict(os.environ)
    environment['PYTHONPATH'] = str(DRILL_ROOT)
    environment['PYTHONIOENCODING'] = 'utf-8'
    print(f'演练目录：{DRILL_ROOT}（复制 {len(copied)} 个文件）')
    completed = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(DRILL_ROOT), env=environment, capture_output=True, text=True,
        encoding='utf-8', errors='replace',
    )
    print(completed.stdout.strip())
    if completed.returncode != 0:
        print(completed.stderr.strip(), file=sys.stderr)
    return completed.returncode


if __name__ == '__main__':
    raise SystemExit(run_drill())
