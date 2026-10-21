"""对话测试：模拟多轮问答，输出每轮事件与回答，便于排查问题。

用法：
    python scripts/chat_test.py --base-url http://127.0.0.1:7880
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

# 输出被重定向到文件/管道时用 UTF-8，避免中文乱码
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass


def ask(client: httpx.Client, base: str, question: str, history: list[dict], timeout: float) -> dict:
    events: list[dict] = []
    with client.stream('POST', base + '/api/ask',
                       json={'question': question, 'history': history, 'run_tag': 'chat-test'},
                       timeout=timeout) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.strip():
                events.append(json.loads(line))
    answer = ''.join(e.get('text', '') for e in events if e.get('stage') == 'answer')
    sources = next((e.get('items') for e in events if e.get('stage') == 'sources'), [])
    notes = next((e.get('notes') for e in events if e.get('stage') == 'evidence'), [])
    query = next((e for e in events if e.get('stage') == 'query'), {})
    error = next((e.get('message') for e in events if e.get('stage') == 'error'), None)
    done = next((e for e in events if e.get('stage') == 'done'), {})
    return {'question': question, 'answer': answer, 'sources': sources, 'notes': notes,
            'rewritten': query.get('rewritten'), 'error': error, 'seconds': done.get('seconds'),
            'stages': [e.get('stage') for e in events]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--base-url', default='http://127.0.0.1:7880')
    parser.add_argument('--timeout', type=float, default=120)
    args = parser.parse_args()
    base = args.base_url.rstrip('/')
    report: list[dict] = []
    history: list[dict] = []

    turns = [
        '实验室危险化学品怎么领用？',
        '那审批人是谁？',
        '我一次想领10天的量可以吗？',
        '科研经费出差住宿费标准是多少？',
        '那北京属于几类地区？',
        '学校游泳池暑假开放吗？',
        '你好，你能做什么？',
    ]

    with httpx.Client() as client:
        for index, question in enumerate(turns, start=1):
            print(f'[{index}/{len(turns)}] {question}', flush=True)
            result = ask(client, base, question, history, args.timeout)
            report.append(result)
            print(f'  rewritten: {result["rewritten"]}')
            print(f'  seconds: {result["seconds"]}  stages: {result["stages"][:4]}...')
            print(f'  answer: {result["answer"][:200]}')
            print(f'  sources: {[(s["id"], s["title"][:16], s.get("article")) for s in result["sources"]][:5]}')
            if result['notes']:
                print(f'  notes: {result["notes"]}')
            if result['error']:
                print(f'  ERROR: {result["error"]}')
            history.append({'role': 'user', 'content': question})
            history.append({'role': 'assistant', 'content': result['answer'][:1200]})

    out = ROOT / 'data' / 'tmp' / ('chat-' + time.strftime('%Y%m%d-%H%M%S') + '.json')
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('\n报告：' + str(out))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
