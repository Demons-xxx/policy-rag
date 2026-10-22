"""OpenAI 兼容生成客户端（契约 3.12）。

- 只用 httpx，不引入额外依赖；
- `stream()` 逐行解析 `data: {...}`，产出 `choices[0].delta.content` 增量；
- 所有异常统一抛 `RuntimeError("生成模型不可用：...")`；
- 韧性（T11）：连接错误 / 5xx / 429 / 首 token 超时会按 `retries` 指数退避重试；
  已输出 token 后的中断不重试（避免重复内容），直接报「回答未完成」；
- `ready()` 用 `GET {base_url}/models` 做 2 秒健康检查，失败时用一次极小
  对话请求兜底（部分 OpenAI 兼容服务没有 /models）。
"""
from __future__ import annotations

import json
import time
from typing import Iterator

import httpx

ERROR_PREFIX = '生成模型不可用：'
DEFAULT_RETRIES = 2
DEFAULT_FIRST_TOKEN_TIMEOUT = 60.0
RETRY_BACKOFF = 0.4  # 秒；第 n 次重试等待 RETRY_BACKOFF * 2**n


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


class _Retryable(Exception):
    """尚未产出任何 token 的可重试错误。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _Incomplete(Exception):
    """已产出 token 后中断：重试会造成重复内容，只能如实报未完成。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ChatClient:
    def __init__(self, base_url: str, model: str, api_key: str = '', timeout: float = 120,
                 thinking: str = 'disabled', retries: int = DEFAULT_RETRIES,
                 first_token_timeout: float = DEFAULT_FIRST_TOKEN_TIMEOUT,
                 transport: httpx.BaseTransport | None = None) -> None:
        self.base_url = (base_url or '').rstrip('/')
        self.model = model
        self.api_key = api_key or ''
        self.timeout = float(timeout)
        self.retries = max(0, int(retries if retries is not None else DEFAULT_RETRIES))
        self.first_token_timeout = max(1.0, float(first_token_timeout or DEFAULT_FIRST_TOKEN_TIMEOUT))
        # 推理型模型（如 deepseek / doubao）默认关闭思考过程，避免答案被思考 token 吃掉；
        # 设为 none 表示不发送该参数（部分 OpenAI 兼容服务不认这个字段）。
        self.thinking = (thinking or 'none').strip().lower()
        self.last_error = ''
        self._headers = {'Content-Type': 'application/json'}
        if self.api_key:
            self._headers['Authorization'] = f'Bearer {self.api_key}'
        self._client = httpx.Client(timeout=httpx.Timeout(self.timeout, connect=10.0),
                                    headers=self._headers, transport=transport)
        # 流式请求单独限制「首 token 等待」：连接与普通请求仍用 timeout
        self._stream_client = httpx.Client(
            timeout=httpx.Timeout(self.timeout, connect=10.0, read=self.first_token_timeout),
            headers=self._headers, transport=transport)

    def _payload(self, messages: list[dict], max_tokens: int, temperature: float,
                 stream: bool) -> dict:
        payload = {
            'model': self.model,
            'messages': messages,
            'stream': bool(stream),
            'max_tokens': int(max_tokens),
            'temperature': float(temperature),
        }
        if self.thinking in ('enabled', 'disabled', 'auto'):
            payload['thinking'] = {'type': self.thinking}
        return payload

    # ---- 内部工具 ----
    def _fail(self, reason: str) -> RuntimeError:
        message = ERROR_PREFIX + reason
        self.last_error = message
        return RuntimeError(message)

    def _url(self, path: str) -> str:
        return f'{self.base_url}/{path.lstrip("/")}'

    def _sleep_before_retry(self, attempt: int) -> None:
        time.sleep(RETRY_BACKOFF * (2 ** attempt))

    def close(self) -> None:
        for client in (self._client, self._stream_client):
            try:
                client.close()
            except Exception:
                pass

    # ---- 契约方法 ----
    def stream(self, messages: list[dict], max_tokens: int = 1024,
               temperature: float = 0.2) -> Iterator[str]:
        if not self.base_url:
            raise self._fail('未配置模型地址（POLICYRAG_LLM_BASE_URL）')
        payload = self._payload(messages, max_tokens, temperature, stream=True)
        attempts = self.retries + 1
        last_reason = ''
        for attempt in range(attempts):
            yielded = False
            try:
                for piece in self._stream_once(payload):
                    yielded = True
                    yield piece
            except _Retryable as exc:
                last_reason = exc.reason
                if yielded:
                    raise self._fail(f'回答未完成：{exc.reason}') from exc
                if attempt >= attempts - 1:
                    break
                self._sleep_before_retry(attempt)
                continue
            except _Incomplete as exc:
                raise self._fail(f'回答未完成：{exc.reason}') from exc
            self.last_error = ''
            return
        raise self._fail(f'{last_reason}（已重试 {self.retries} 次）')

    def _stream_once(self, payload: dict) -> Iterator[str]:
        try:
            with self._stream_client.stream(
                    'POST', self._url('chat/completions'), json=payload) as response:
                if response.status_code >= 400:
                    body = response.read().decode('utf-8', 'replace')
                    reason = f'HTTP {response.status_code} {_brief(body)}'
                    if response.status_code >= 500 or response.status_code == 429:
                        raise _Retryable(reason)
                    raise self._fail(reason)
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
        except (RuntimeError, _Retryable, _Incomplete):
            raise
        except httpx.TimeoutException as exc:
            raise _Retryable(f'等待模型响应超时（{self.first_token_timeout:g}s）：{_reason(exc)}') from exc
        except httpx.HTTPError as exc:  # 连接失败、读流中断、协议错误等
            raise _Retryable(f'无法连接模型服务：{_reason(exc)}') from exc
        except Exception as exc:  # 兜底：编码等异常
            raise _Retryable(_reason(exc)) from exc

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
        attempts = self.retries + 1
        last_reason = ''
        response = None
        for attempt in range(attempts):
            try:
                response = self._client.post(self._url('chat/completions'), json=payload)
            except httpx.HTTPError as exc:
                last_reason = f'无法连接模型服务：{_reason(exc)}'
                if attempt < attempts - 1:
                    self._sleep_before_retry(attempt)
                    continue
                break
            if response.status_code >= 400:
                reason = f'HTTP {response.status_code} {_brief(response.text)}'
                if response.status_code >= 500 or response.status_code == 429:
                    last_reason = reason
                    if attempt < attempts - 1:
                        self._sleep_before_retry(attempt)
                        continue
                    break
                raise self._fail(reason)
            break
        if response is None or response.status_code >= 400:
            raise self._fail(f'{last_reason}（已重试 {self.retries} 次）')
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
