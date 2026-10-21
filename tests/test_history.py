"""多轮历史清洗（通用规则，与具体问题无关）。

- 历史回答里的 [证据n] 是上一轮编号，带进本轮会和新编号冲突，需要清掉；
- 超长历史在句末标点处截断，避免模型看到半句话。
"""
from app.llm import prompts


def test_assistant_history_drops_stale_citation_numbers():
    history = [{'role': 'user', 'content': '危化品怎么领用？'},
               {'role': 'assistant', 'content': '实行线上申请、导师审批 [证据1][证据3]。'}]
    cleaned = prompts.normalize_history(history)
    assert cleaned[-1]['content'] == '实行线上申请、导师审批。'


def test_user_history_keeps_citations_untouched():
    history = [{'role': 'user', 'content': '第十二条 [证据2] 说了什么'}]
    cleaned = prompts.normalize_history(history)
    assert '[证据2]' in cleaned[0]['content']


def test_long_assistant_history_clips_at_sentence_end():
    long_text = '这是第一句结论。' + '细节说明' * 400
    history = [{'role': 'user', 'content': '问题'},
               {'role': 'assistant', 'content': long_text}]
    cleaned = prompts.normalize_history(history)
    assert cleaned[-1]['content'].endswith('…')
    assert cleaned[-1]['content'].rstrip('…').endswith('。')


def test_history_budget_still_respected():
    history = []
    for index in range(12):
        history.append({'role': 'user', 'content': f'第{index}问' + '内容' * 300})
        history.append({'role': 'assistant', 'content': f'第{index}答' + '内容' * 300})
    cleaned = prompts.normalize_history(history)
    assert sum(len(item['content']) for item in cleaned) <= prompts.config.MAX_HISTORY_CHARS
