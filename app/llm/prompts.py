"""政策问答提示词（契约 3.14）。

系统提示词是回答质量与合规的底线：只用编号证据、逐条标注 [证据n]、无依据
必须明确拒答、不得编造文号/条款号/日期、新旧版本以现行版本为准。
"""
from __future__ import annotations

from app import config

# 最近保留的对话轮数（一问一答算一轮），与契约“最近 6 轮”一致。
MAX_HISTORY_TURNS = 6
# 单条历史消息的最大字符数，避免一条超长回答吃掉全部预算。
MAX_HISTORY_ITEM_CHARS = 1200

SYSTEM_PROMPT = """你是高校政策与制度智能问答助手，只回答制度、办法、通知与办事流程相关的问题。

必须严格遵守：
1. 只使用【检索到的制度证据】中的内容作答，不得使用证据之外的知识，不得凭印象或常识补充。
2. 每一个事实、结论、数字、时限、条件、办理地点后面都必须标注来源，格式为 [证据n]（n 为证据编号）；一句话涉及多条证据时写 [证据1][证据3]。
3. 如果证据不足以回答，必须直接写出：“未在现行有效制度中找到依据，建议咨询XX部门”，其中 XX 取自证据里的归口部门；证据中没有部门信息时写“相关归口部门”。不要猜测，不要用“一般来说”“通常情况下”等说法兜底。
4. 禁止编造条款号、文号、生效日期、部门名称、金额、比例和流程步骤。只能引用证据原文中明确出现的信息；原文没有的，宁可不写。
5. 同一事项存在新旧版本时，以现行有效版本（status 为 active、或生效日期更晚且未废止）为准，并简要说明“旧版《……》相关内容已不适用”。
6. 回答使用简体中文，简洁分点，先给结论再给依据；不要整段复述原文；不要输出思考过程、推理过程，也不要输出 ＜think＞ 标签。
7. 只输出回答正文，不要输出“根据检索结果”“以下是回答”之类开场白，不要输出与问题无关的免责声明。
8. 若问题与制度无关（寒暄、闲聊、与制度无关的通用知识），用一两句话说明你只能回答制度与办事流程问题，并引导用户提出具体制度问题。
"""

CHAT_SYSTEM_PROMPT = """你是高校政策与制度智能问答助手。当前问题与制度检索无关（寒暄或闲聊）。
请用简体中文简短、礼貌地回答，不要编造任何制度、条款、文号、日期或部门信息；
如果用户实际想咨询制度问题，请引导其说明具体事项（例如“实验室危化品怎么领用”）。
不要输出思考过程，只输出回答正文。
"""


def _clip(text: str, limit: int) -> str:
    text = str(text or '').strip()
    if len(text) <= limit:
        return text
    return text[:limit] + '…'


def normalize_history(history: list[dict] | None,
                      max_chars: int | None = None) -> list[dict]:
    """清洗并裁剪历史：最近 6 轮、总字符数不超过 MAX_HISTORY_CHARS。

    - 只保留 user / assistant 角色，丢弃空消息；
    - 从最新往前累计字符预算，超预算的旧消息直接丢弃；
    - 裁剪后若以 assistant 开头，去掉开头这几条，保证 user/assistant 交替；
    - 最终条数同时不超过 config.MAX_HISTORY_MESSAGES。
    """
    budget = config.MAX_HISTORY_CHARS if max_chars is None else int(max_chars)
    max_messages = max(2, int(config.MAX_HISTORY_MESSAGES or 12))
    max_messages = min(max_messages, MAX_HISTORY_TURNS * 2)

    cleaned: list[dict] = []
    for item in history or []:
        if not isinstance(item, dict):
            continue
        role = str(item.get('role') or '').strip().lower()
        if role not in ('user', 'assistant'):
            continue
        content = _clip(item.get('content') or '', MAX_HISTORY_ITEM_CHARS)
        if not content:
            continue
        cleaned.append({'role': role, 'content': content})

    cleaned = cleaned[-max_messages:]

    kept: list[dict] = []
    used = 0
    for message in reversed(cleaned):
        length = len(message['content'])
        if kept and used + length > budget:
            break
        if not kept and length > budget:  # 最新一条超预算时截断保留
            message = {'role': message['role'], 'content': message['content'][:budget]}
            length = len(message['content'])
        kept.append(message)
        used += length
    kept.reverse()

    while kept and kept[0]['role'] == 'assistant':
        kept.pop(0)
    return kept


def _user_content(question: str, context: str) -> str:
    evidence = (context or '').strip()
    if not evidence:
        evidence = '（本次没有检索到任何制度证据）'
    return (
        '【检索到的制度证据】\n'
        f'{evidence}\n\n'
        '【用户问题】\n'
        f'{question.strip()}\n\n'
        '【回答要求】\n'
        '严格依据上面的编号证据回答，每个事实后标注 [证据n]；'
        '如果证据不足以回答，直接说明“未在现行有效制度中找到依据，建议咨询XX部门”，不要编造条款号、文号或日期；'
        '只输出回答正文，不要输出思考过程。'
    )


def build_messages(question: str, context: str, history: list[dict]) -> list[dict]:
    """组装一次问答的 messages：系统提示词 + 裁剪后的历史 + 带证据的问题。"""
    messages = [{'role': 'system', 'content': SYSTEM_PROMPT}]
    messages.extend(normalize_history(history))
    messages.append({'role': 'user', 'content': _user_content(question, context)})
    return messages


def build_chat_messages(question: str, history: list[dict]) -> list[dict]:
    """闲聊/寒暄模式：不注入证据，只做简短礼貌回复。"""
    messages = [{'role': 'system', 'content': CHAT_SYSTEM_PROMPT}]
    messages.extend(normalize_history(history))
    messages.append({'role': 'user', 'content': str(question or '').strip()})
    return messages
