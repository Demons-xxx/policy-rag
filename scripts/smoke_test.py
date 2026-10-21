"""端到端冒烟测试：检查服务、检索、引用、拒答、多轮与原文页。

用法：
    .\\.venv\\Scripts\\python.exe scripts/smoke_test.py
    .\\.venv\\Scripts\\python.exe scripts/smoke_test.py --base-url http://127.0.0.1:7880 --timeout 240

退出码：0 全部通过；1 有失败项。报告写入 data/tmp/smoke-<时间>.json。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import config  # noqa: E402


def ask(client: httpx.Client, base: str, question: str, history: list[dict],
        timeout: float) -> dict:
    """调用 /api/ask 并归集 NDJSON 事件。"""
    events: list[dict] = []
    payload = {'question': question, 'history': history, 'run_tag': 'smoke'}
    with client.stream('POST', base + '/api/ask', json=payload, timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            line = line.strip()
            if not line:
                continue
            events.append(json.loads(line))
    answer = ''.join(e.get('text', '') for e in events if e.get('stage') == 'answer')
    sources = next((e.get('items') for e in events if e.get('stage') == 'sources'), [])
    error = next((e.get('message') for e in events if e.get('stage') == 'error'), None)
    done = next((e for e in events if e.get('stage') == 'done'), {})
    return {'question': question, 'answer': answer, 'sources': sources,
            'error': error, 'seconds': done.get('seconds'), 'events': [e.get('stage') for e in events]}


def main() -> int:
    parser = argparse.ArgumentParser(description='PolicyRAG 端到端冒烟测试')
    parser.add_argument('--base-url', default='http://127.0.0.1:7880')
    parser.add_argument('--timeout', type=float, default=240)
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    checks: list[dict] = []
    report: dict = {'base_url': base, 'started_at': time.strftime('%Y-%m-%d %H:%M:%S'), 'checks': checks}

    def record(name: str, ok: bool, detail: str = '') -> None:
        checks.append({'name': name, 'ok': bool(ok), 'detail': str(detail)[:500]})
        print(('PASS ' if ok else 'FAIL ') + name + ((' | ' + str(detail)[:200]) if detail and not ok else ''))

    with httpx.Client() as client:
        # 1. 服务信息
        info = client.get(base + '/api/info', timeout=20).json()
        record('服务信息可读', True, '')
        record('知识库已发布', bool(info.get('release_id')) and info.get('doc_count', 0) > 0,
               f"release={info.get('release_id')} docs={info.get('doc_count')} units={info.get('unit_count')}")
        record('生成模型就绪', bool(info.get('model_ready')), info.get('error') or '')
        report['info'] = info

        # 2. 文档与 PDF 资源
        documents = client.get(base + '/api/documents', timeout=20).json().get('documents', [])
        record('文档列表非空', len(documents) > 0, f'{len(documents)} 份')
        pdf_docs = [d for d in documents if d.get('has_pdf')]
        report['documents'] = documents
        if pdf_docs:
            doc_id, pages = pdf_docs[0]['id'], pdf_docs[0].get('pages') or 1
            pdf = client.get(f'{base}/api/documents/{doc_id}/pdf', timeout=30)
            record('PDF 原文可下载', pdf.status_code == 200 and pdf.content[:4] == b'%PDF', pdf.status_code)
            image = client.get(f'{base}/api/documents/{doc_id}/pages/1/image', timeout=60)
            record('PDF 页图可渲染', image.status_code == 200 and image.content[:4] == b'\x89PNG', image.status_code)
        else:
            record('PDF 原文可下载', False, '没有 has_pdf=true 的文档')
            record('PDF 页图可渲染', False, '没有 has_pdf=true 的文档')

        # 3. 直接问答（要求引用）
        first = ask(client, base,
                    '实验室危险化学品领用需要哪些人审批？单次最多领几天用量？', [], args.timeout)
        record('直接问答返回回答', bool(first['answer']) and not first['error'], first['error'] or '')
        record('回答带引用标记', '[证据' in first['answer'], first['answer'][:120])
        record('回答命中关键词(3个工作日)', '3' in first['answer'] and '工作日' in first['answer'], first['answer'][:200])
        record('sources 非空且字段齐全', bool(first['sources']) and all(
            {'id', 'doc_id', 'file', 'pages', 'focus_page'} <= set(s) for s in first['sources']),
            first['sources'][:1])
        report['direct'] = first

        # 4. 拒答（知识库外问题）
        refusal = ask(client, base, '请问学校游泳池暑假开放时间和票价是多少？', [], args.timeout)
        refused = any(word in refusal['answer'] for word in ('未在现行', '未找到', '没有找到', '无法回答'))
        record('库外问题明确拒答', refused, refusal['answer'][:160])
        report['refusal'] = refusal

        # 5. 多轮追问（依赖上一轮主体）
        history = [{'role': 'user', 'content': first['question']},
                   {'role': 'assistant', 'content': first['answer'][:1500]}]
        follow = ask(client, base, '那审批人具体是谁？', history, args.timeout)
        record('多轮追问有回答', bool(follow['answer']) and not follow['error'], follow['error'] or '')
        record('多轮补全主体', any(word in follow['answer'] for word in ('导师', '审批')), follow['answer'][:160])
        report['followup'] = follow

    failed = [c for c in checks if not c['ok']]
    report['finished_at'] = time.strftime('%Y-%m-%d %H:%M:%S')
    report['passed'] = len(checks) - len(failed)
    report['total'] = len(checks)
    out_dir = config.DATA_DIR / 'tmp'
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / ('smoke-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(f'\n{report["passed"]}/{report["total"]} 通过，报告：{out}')
    return 0 if not failed else 1


if __name__ == '__main__':
    raise SystemExit(main())
