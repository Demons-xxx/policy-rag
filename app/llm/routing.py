"""意图路由与多轮问句改写（契约 3.13）。

`route()` 让模型判断问题该走制度检索还是直接闲聊，并给出检索问句；
`rewrite()` 做指代消解与省略补全（“那第十二条说什么” → “《XX办法》第十二条说什么”）。
两者都必须在生成模型不可用时优雅降级：路由回退到 policy，改写回退到原问题。
"""
from __future__ import annotations

import json
import re

from app.llm import prompts
from app.llm.client import ChatClient

ROUTE_SYSTEM = """你是高校政策问答系统的意图路由模块。判断用户当前这句话属于哪一类：
- policy：咨询制度、办法、通知、条款、办事流程、材料、时限、归口部门等需要查制度库的问题；
- chat：寒暄、感谢、与制度无关的闲聊，或没有信息量的追问。

同时给出用于检索知识库的问句 search_query：保留问题原意，补上必要的限定词（如制度名称、条款号、事项名称），不要加引号或解释。

只输出 JSON，不要输出代码块、不要解释，格式固定为：
{"mode": "policy 或 chat", "search_query": "检索问句", "reason": "一句话理由"}
"""

REWRITE_SYSTEM = """你是高校政策问答系统的问句改写模块，负责多轮对话中的指代消解与省略补全。
把用户当前问题改写成一句可以独立检索制度库的完整问句：
- 把“这个/那个/它/上面说的/那第十二条”等指代补全成对话历史里明确的制度名称、条款号或事项；
- 补全省略的主语、对象和动作；
- 不要回答问题，不要解释，不要添加历史里没有的信息；
- 只输出改写后的问句，不要输出引号、编号或任何前缀。
"""

_JSON_BLOCK = re.compile(r'\{.*\}', re.DOTALL)
_FENCE = re.compile(r'^```(?:json)?\s*|\s*```$', re.IGNORECASE)
_PREFIX = re.compile(r'^(?:改写(?:后)?(?:的)?(?:检索)?问句|检索问句|改写|问题|答案)\s*[:：]\s*')
_TRAILING = '。；;，, \t\r\n'
_MODE_CHAT = {'chat', 'chitchat', 'chat_mode', 'greeting', '闲聊', '寒暄', '问候'}
_MODE_POLICY = {'policy', 'retrieve', 'search', 'rag', 'knowledge', '制度', '检索'}


def _fallback(question: str, reason: str) -> dict:
    return {'mode': 'policy', 'search_query': question, 'reason': reason}


def _parse_json(text: str) -> dict | None:
    """从模型输出里尽量抠出 JSON 对象；失败返回 None。"""
    raw = _FENCE.sub('', str(text or '').strip()).strip()
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK.search(raw)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
        return value if isinstance(value, dict) else None
    except json.JSONDecodeError:
        return None


def _recent_history(history: list[dict], limit: int = 4) -> list[dict]:
    items: list[dict] = []
    for message in history or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get('role') or '').strip().lower()
        content = str(message.get('content') or '').strip()
        if role in ('user', 'assistant') and content:
            items.append({'role': role, 'content': content[:300]})
    return items[-limit:]


def route(question: str, history: list[dict], llm: ChatClient) -> dict:
    """判断问题走向，返回 {"mode": "policy"|"chat", "search_query": str, "reason": str}。

    契约 3.13 里 mode 写的是 retrieve|chat；本实现统一输出 policy|chat（两者等价，
    调用方以 `mode == "chat"` 判断闲聊）。模型返回 retrieve/policy 都会归一成 policy。
    """
    question = (question or '').strip()
    if not question:
        return _fallback(question, '问题为空，按制度检索处理')

    messages = [{'role': 'system', 'content': ROUTE_SYSTEM}]
    messages.extend(_recent_history(history))
    messages.append({'role': 'user', 'content': f'当前问题：{question}'})

    try:
        text = llm.complete(messages, max_tokens=160, temperature=0.0)
    except Exception as exc:
        return _fallback(question, f'路由模型不可用，按制度检索处理（{type(exc).__name__}）')

    parsed = _parse_json(text)
    if parsed is None:
        return _fallback(question, '路由结果无法解析，按制度检索处理')

    raw_mode = str(parsed.get('mode') or '').strip().lower()
    if raw_mode in _MODE_CHAT:
        mode = 'chat'
    elif raw_mode in _MODE_POLICY:
        mode = 'policy'
    else:
        mode = 'policy'

    search_query = str(parsed.get('search_query') or '').strip().strip('"“”\'')
    if not search_query:
        search_query = question
    search_query = search_query[:200]
    reason = str(parsed.get('reason') or '').strip()[:120]
    return {'mode': mode, 'search_query': search_query, 'reason': reason}


def rewrite(question: str, history: list[dict], llm: ChatClient) -> str:
    """多轮问句补全；没有历史或模型不可用时原样返回问题。"""
    question = (question or '').strip()
    if not question:
        return question
    recent = _recent_history(prompts.normalize_history(history), limit=6)
    if not recent:
        # 没有上下文就谈不上指代消解，直接返回可以省一次模型调用。
        return question

    messages = [{'role': 'system', 'content': REWRITE_SYSTEM}]
    messages.extend(recent)
    messages.append({
        'role': 'user',
        'content': f'请结合上面的对话历史，只输出改写后的检索问句。\n当前问题：{question}',
    })
    try:
        text = llm.complete(messages, max_tokens=160, temperature=0.0)
    except Exception:
        return question

    lines = [line.strip() for line in str(text or '').splitlines() if line.strip()]
    if not lines:
        return question
    rewritten = lines[0]
    rewritten = _PREFIX.sub('', rewritten).strip()
    rewritten = rewritten.strip('"“”\'`').strip(_TRAILING)
    if len(rewritten) < 2 or len(rewritten) > 300:
        return question
    return rewritten
