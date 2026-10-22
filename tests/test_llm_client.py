"""生成客户端韧性（T11）：5xx/429/首 token 超时重试、流中断只报未完成。

用 httpx.MockTransport 模拟服务端，不联网、不起真实模型。
"""
from __future__ import annotations

import json
import types

import httpx
import pytest

from app.llm.client import ChatClient


def _sse(*pieces: str) -> bytes:
    lines = [f"data: {json.dumps({'choices': [{'delta': {'content': piece}}]}, ensure_ascii=False)}\n\n"
             for piece in pieces]
    lines.append("data: [DONE]\n\n")
    return ''.join(lines).encode('utf-8')


def _client(handler, **kwargs) -> ChatClient:
    return ChatClient('http://mock/v1', 'mock-model', transport=httpx.MockTransport(handler),
                      retries=kwargs.pop('retries', 2),
                      first_token_timeout=kwargs.pop('first_token_timeout', 5), **kwargs)


def test_stream_retries_after_500_then_succeeds():
    calls = {'count': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        if calls['count'] == 1:
            return httpx.Response(500, text='upstream exploded')
        return httpx.Response(200, content=_sse('你好', '世界'))

    client = _client(handler)
    assert ''.join(client.stream([{'role': 'user', 'content': 'hi'}])) == '你好世界'
    assert calls['count'] == 2
    assert client.last_error == ''


def test_stream_exhausts_retries_with_chinese_reason():
    calls = {'count': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        return httpx.Response(502, text='bad gateway')

    client = _client(handler, retries=2)
    with pytest.raises(RuntimeError) as info:
        list(client.stream([{'role': 'user', 'content': 'hi'}]))
    assert 'HTTP 502' in str(info.value)
    assert '已重试 2 次' in str(info.value)
    assert calls['count'] == 3


def test_stream_retries_connection_error():
    calls = {'count': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        if calls['count'] == 1:
            raise httpx.ConnectError('connection refused')
        return httpx.Response(200, content=_sse('ok'))

    client = _client(handler)
    assert ''.join(client.stream([{'role': 'user', 'content': 'hi'}])) == 'ok'
    assert calls['count'] == 2


def test_stream_retries_first_token_timeout():
    calls = {'count': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        if calls['count'] == 1:
            raise httpx.ReadTimeout('timed out waiting for first token')
        return httpx.Response(200, content=_sse('迟到的回答'))

    client = _client(handler)
    assert ''.join(client.stream([{'role': 'user', 'content': 'hi'}])) == '迟到的回答'
    assert calls['count'] == 2


class _BrokenResponse:
    status_code = 200

    def __init__(self) -> None:
        self.lines = iter([
            "data: " + json.dumps({'choices': [{'delta': {'content': '半句'}}]}, ensure_ascii=False),
            None,  # 占位，下一行触发异常
        ])

    def read(self) -> bytes:
        return b''

    def iter_lines(self):
        yield next(self.lines)
        raise httpx.ReadError('connection reset mid-stream')


class _BrokenContext:
    def __enter__(self):
        return _BrokenResponse()

    def __exit__(self, *exc_info):
        return False


def test_stream_midstream_interrupt_is_not_retried():
    client = _client(lambda request: httpx.Response(200, content=_sse('x')))
    client._stream_client = types.SimpleNamespace(stream=lambda *args, **kwargs: _BrokenContext())

    received: list[str] = []
    with pytest.raises(RuntimeError) as info:
        for piece in client.stream([{'role': 'user', 'content': 'hi'}]):
            received.append(piece)
    assert received == ['半句']
    assert '回答未完成' in str(info.value)
    assert '已重试' not in str(info.value)


def test_4xx_is_not_retried():
    calls = {'count': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        return httpx.Response(401, text='unauthorized')

    client = _client(handler)
    with pytest.raises(RuntimeError) as info:
        list(client.stream([{'role': 'user', 'content': 'hi'}]))
    assert 'HTTP 401' in str(info.value)
    assert calls['count'] == 1


def test_complete_retries_5xx_then_succeeds():
    calls = {'count': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        if calls['count'] == 1:
            return httpx.Response(503, text='busy')
        return httpx.Response(200, json={'choices': [{'message': {'content': '答案'}}]})

    client = _client(handler)
    assert client.complete([{'role': 'user', 'content': 'hi'}]) == '答案'
    assert calls['count'] == 2


def test_complete_exhausts_retries():
    calls = {'count': 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls['count'] += 1
        return httpx.Response(500, text='boom')

    client = _client(handler, retries=1)
    with pytest.raises(RuntimeError) as info:
        client.complete([{'role': 'user', 'content': 'hi'}])
    assert '已重试 1 次' in str(info.value)
    assert calls['count'] == 2
