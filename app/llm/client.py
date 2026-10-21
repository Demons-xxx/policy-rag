"""OpenAI 兼容生成客户端（契约 3.12）。

- 只用 httpx，不引入额外依赖；
- `stream()` 逐行解析 `data: {...}`，产出 `choices[0].delta.content` 增量；
- 所有异常统一抛 `RuntimeError("生成模型不可用：...")`；
- `ready()` 用 `GET {base_url}/models` 做 2 秒健康检查，失败时用一次极小
  对话请求兜底（部分 OpenAI 兼容服务没有 /models）。
"""
from __future__ import annotations

import json
from typing import Iterator

import httpx

ERROR_PREFIX = '生成模型不可用：'


def _brief(text: str, limit: int = 200) -> str:
    """压缩服务端返回的错误正文，避免把整页 HTML 塞进中文提示。"""
    one_line = ' '.join(str(text or '').split())
    return one_line[:limit] + ('…' if len(one_line) > limit else '')


def _reason(exc: BaseException) -> str:
    name = type(exc).__name__
    detail = _brief(str(exc), 160)
    return f'{name}: {detail}' if detail else name


def _content_text(content) -> str:
    """兼容 `content` 为字符串 / 分段列表 / None 的多种服务端实现。"""
    if content is None:
        return ''
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get('text') or item.get('content')
                if isinstance(text, str):
                    parts.append(text)
        return ''.join(parts)
    return str(content)


class ChatClient:
    def __init__(self, base_url: str, model: str, api_key: str = '', timeout: float = 120) -> None:
        self.base_url = (base_url or '').rstrip('/')
        self.model = model
        self.api_key = api_key or ''
        self.timeout = float(timeout)
        self.last_error = ''
        self._headers = {'Content-Type': 'application/json'}
        if self.api_key:
            self._headers['Authorization'] = f'Bearer {self.api_key}'
        self._client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0))

    # ---- 内部工具 ----
    def _fail(self, reason: str) -> RuntimeError:
        message = ERROR_PREFIX + reason
        self.last_error = message
        return RuntimeError(message)

    def _url(self, path: str) -> str:
        return f'{self.base_url}/{path.lstrip("/")}'

    def close(self) -> None:
        try:
            self._client.close()
        except Exception:
            pass

    # ---- 契约方法 ----
    def stream(self, messages: list[dict], max_tokens: int = 1024,
               temperature: float = 0.2) -> Iterator[str]:
        if not self.base_url:
            raise self._fail('未配置模型地址（POLICYRAG_LLM_BASE_URL）')
        payload = {
            'model': self.model,
            'messages': messages,
            'stream': True,
            'max_tokens': int(max_tokens),
            'temperature': float(temperature),
        }
        try:
            with self._client.stream('POST', self._url('chat/completions'), json=payload) as response:
                if response.status_code >= 400:
                    body = response.read().decode('utf-8', 'replace')
                    raise self._fail(f'HTTP {response.status_code} {_brief(body)}')
                for raw_line in response.iter_lines():
                    line = (raw_line or '').strip()
                    if not line or line.startswith(':') or not line.startswith('data:'):
                        continue
                    data = line[len('data:'):].strip()
                    if data in ('[DONE]', 'DONE'):
                        break
                    try:
                        chunk = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(chunk, dict):
                        continue
                    error = chunk.get('error')
                    if error:
                        detail = error.get('message') if isinstance(error, dict) else error
                        raise self._fail(f'服务端返回错误：{_brief(str(detail))}')
                    for choice in chunk.get('choices') or []:
                        if not isinstance(choice, dict):
                            continue
                        delta = choice.get('delta') or {}
                        text = _content_text(delta.get('content'))
                        if text:
                            yield text
                        if not text:
                            # 少数实现忽略 stream 参数，直接把完整答案放在 message 里
                            text = _content_text((choice.get('message') or {}).get('content'))
                            if text:
                                yield text
        except RuntimeError:
            raise
        except Exception as exc:  # httpx 网络错误、读流中断、JSON 异常等
            raise self._fail(_reason(exc)) from exc

    def complete(self, messages: list[dict], max_tokens: int = 512,
                 temperature: float = 0.1) -> str:
        if not self.base_url:
            raise self._fail('未配置模型地址（POLICYRAG_LLM_BASE_URL）')
        payload = {
            'model': self.model,
            'messages': messages,
            'stream': False,
            'max_tokens': int(max_tokens),
            'temperature': float(temperature),
        }
        try:
            response = self._client.post(self._url('chat/completions'), json=payload)
        except Exception as exc:
            raise self._fail(_reason(exc)) from exc
        if response.status_code >= 400:
            raise self._fail(f'HTTP {response.status_code} {_brief(response.text)}')
        try:
            data = response.json()
        except Exception as exc:
            raise self._fail(f'返回内容不是合法 JSON（{_reason(exc)}）') from exc
        if isinstance(data, dict) and data.get('error'):
            detail = data['error'].get('message') if isinstance(data['error'], dict) else data['error']
            raise self._fail(f'服务端返回错误：{_brief(str(detail))}')
        choices = (data or {}).get('choices') or []
        text = ''
        if choices:
            text = _content_text((choices[0].get('message') or {}).get('content'))
        if not text.strip():
            raise self._fail('模型返回了空内容')
        self.last_error = ''
        return text

    def ready(self) -> bool:
        """2 秒内判断模型服务是否可用；任何异常都返回 False，不向外抛。"""
        if not self.base_url:
            self.last_error = ERROR_PREFIX + '未配置模型地址（POLICYRAG_LLM_BASE_URL）'
            return False
        try:
            response = self._client.get(self._url('models'), timeout=2.0)
            if response.status_code < 400:
                self.last_error = ''
                return True
            self.last_error = ERROR_PREFIX + f'健康检查 HTTP {response.status_code}'
        except Exception as exc:
            self.last_error = ERROR_PREFIX + f'无法连接 {self.base_url}（{_reason(exc)}）'
        try:
            response = self._client.post(
                self._url('chat/completions'),
                json={'model': self.model,
                      'messages': [{'role': 'user', 'content': 'ping'}],
                      'max_tokens': 1, 'temperature': 0.0, 'stream': False},
                timeout=2.0,
            )
            if response.status_code < 400:
                self.last_error = ''
                return True
            self.last_error = ERROR_PREFIX + f'健康检查 HTTP {response.status_code}'
        except Exception as exc:
            self.last_error = ERROR_PREFIX + f'无法连接 {self.base_url}（{_reason(exc)}）'
        return False
