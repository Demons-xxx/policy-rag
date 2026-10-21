"""本地假大模型：没有真实模型时用于端到端自测。

启动：
    .\\.venv\\Scripts\\python.exe scripts/mock_llm.py --port 8081

它实现 OpenAI 兼容的 /v1/models 与 /v1/chat/completions（含流式），
会识别路由请求并返回合法 JSON，其余请求返回带 [证据1] 的固定中文回答。
仅用于开发联调，不要在生产环境使用。
"""
from __future__ import annotations

import argparse
import json
import re
import time
from pathlib import Path
import sys

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
import uvicorn

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

app = FastAPI(title='Mock LLM')


def _last_user(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get('role') == 'user':
            return message.get('content') or ''
    return ''


def _looks_like_routing(messages: list[dict]) -> bool:
    text = '\n'.join(str(m.get('content') or '') for m in messages)
    return ('JSON' in text or 'json' in text) and ('mode' in text) and ('search_query' in text)


def _looks_like_rewrite(messages: list[dict]) -> bool:
    text = '\n'.join(str(m.get('content') or '') for m in messages)
    return '改写' in text and '只输出' in text


def _routing_reply(messages: list[dict]) -> str:
    question = _last_user(messages)
    match = re.search(r'用户(?:问题|输入)[:：]\s*(.+)', question)
    query = (match.group(1) if match else question).strip().splitlines()[0][:120]
    return json.dumps({'mode': 'policy', 'search_query': query, 'reason': 'mock'}, ensure_ascii=False)


def _rewrite_reply(messages: list[dict]) -> str:
    question = _last_user(messages)
    match = re.search(r'用户(?:问题|输入)[:：]\s*(.+)', question)
    return (match.group(1) if match else question).strip().splitlines()[0][:200]


def _answer_reply(messages: list[dict]) -> str:
    question = _last_user(messages)
    context = ''
    for message in messages:
        if message.get('role') == 'system':
            context = message.get('content') or ''
    titles = re.findall(r'\[证据(\d+)\]\s*([^\n（(]+)', context)
    lines = [f'关于「{question[:40]}」，根据现行制度：', '']
    if titles:
        for index, title in titles[:2]:
            lines.append(f'{index}. 相关条款出自{title.strip()}，具体条件与办理要求以原文为准。[证据{index}]')
    else:
        lines.append('未在现行有效制度中找到依据，建议咨询相关归口部门。')
    lines.append('')
    lines.append('（本回答由 mock 模型生成，仅用于联调测试。）')
    return '\n'.join(lines)


def _reply(messages: list[dict]) -> str:
    if _looks_like_routing(messages):
        return _routing_reply(messages)
    if _looks_like_rewrite(messages):
        return _rewrite_reply(messages)
    return _answer_reply(messages)


@app.get('/v1/models')
def models():
    return {'object': 'list', 'data': [{'id': 'mock-llm', 'object': 'model'}]}


@app.get('/health')
def health():
    return {'ok': True}


@app.post('/v1/chat/completions')
async def chat_completions(request: Request):
    body = await request.json()
    messages = body.get('messages') or []
    text = _reply(messages)
    model = body.get('model', 'mock-llm')
    if not body.get('stream'):
        return {
            'id': 'chatcmpl-mock', 'object': 'chat.completion', 'created': int(time.time()),
            'model': model,
            'choices': [{'index': 0, 'message': {'role': 'assistant', 'content': text}, 'finish_reason': 'stop'}],
            'usage': {'prompt_tokens': 0, 'completion_tokens': 0, 'total_tokens': 0},
        }

    def generate():
        for index in range(0, len(text), 12):
            chunk = text[index:index + 12]
            payload = {'id': 'chatcmpl-mock', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                       'model': model, 'choices': [{'index': 0, 'delta': {'content': chunk}, 'finish_reason': None}]}
            yield 'data: ' + json.dumps(payload, ensure_ascii=False) + '\n\n'
            time.sleep(0.01)
        done = {'id': 'chatcmpl-mock', 'object': 'chat.completion.chunk', 'created': int(time.time()),
                'model': model, 'choices': [{'index': 0, 'delta': {}, 'finish_reason': 'stop'}]}
        yield 'data: ' + json.dumps(done, ensure_ascii=False) + '\n\n'
        yield 'data: [DONE]\n\n'

    return StreamingResponse(generate(), media_type='text/event-stream')


def main() -> None:
    parser = argparse.ArgumentParser(description='本地假大模型（仅联调）')
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8081)
    args = parser.parse_args()
    uvicorn.run(app, host=args.host, port=args.port, log_level='warning')


if __name__ == '__main__':
    main()
