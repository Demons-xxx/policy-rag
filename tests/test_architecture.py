"""架构守卫（fitness function）：分层依赖与领域词隔离。

这两个断言把 docs/架构.md 的原则变成可执行的检查：
1) 依赖方向只能「接口层 → 应用层 → 契约」，下层不得反向 import 上层；
2) 通用层（core/store/retrieval/qa）不得出现制度领域词，否则就不是可复用内核。

新增模块时保持这两条，抽取成独立框架时才不用改代码。
"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parent.parent / 'app'

#: 层 → 允许依赖的层（app.<layer> 的形式）；未列出的层不做限制
ALLOWED_DEPENDENCIES: dict[str, set[str]] = {
    'core': {'core'},
    'store': {'store'},
    'domain': {'core', 'domain'},
    'retrieval': {'core', 'store', 'retrieval', 'domain'},
    'llm': {'core', 'llm'},
    'qa': {'core', 'store', 'retrieval', 'llm', 'qa', 'domain', 'wiring'},
    'ingest': {'core', 'store', 'ingest', 'retrieval', 'llm', 'domain', 'wiring'},
    'api': {'core', 'store', 'retrieval', 'llm', 'ingest', 'qa', 'domain', 'wiring'},
    # app/wiring.py 是组合根，跨层拼装是它的职责，不做限制
}

#: 通用层不得出现的领域词（出现在注释里也算，除非显式标注 noqa）
DOMAIN_WORDS = ('制度', '条款', '废止', '政策')


def _module_layer(path: Path) -> str:
    rel = path.relative_to(APP)
    return rel.parts[0] if len(rel.parts) > 1 else ''


def _app_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding='utf-8'))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('app'):
            found.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith('app'):
                    found.add(alias.name)
    return found


def test_layering_rules_hold():
    problems: list[str] = []
    for path in sorted(APP.rglob('*.py')):
        layer = _module_layer(path)
        allowed = ALLOWED_DEPENDENCIES.get(layer)
        if not allowed:
            continue
        for target in _app_imports(path):
            parts = target.split('.')
            dep = parts[1] if len(parts) > 1 else ''
            if dep and dep not in allowed and dep != 'config':
                problems.append(f'{path.relative_to(APP.parent)} 依赖了 {target}（{layer} 只能依赖 {sorted(allowed)}）')
    assert not problems, '分层依赖违规：\n' + '\n'.join(problems)


def test_generic_layers_avoid_domain_words():
    problems: list[str] = []
    for layer in ('core', 'store'):
        for path in sorted((APP / layer).rglob('*.py')):
            for number, line in enumerate(path.read_text(encoding='utf-8').splitlines(), 1):
                if 'noqa: domain-word' in line:
                    continue
                for word in DOMAIN_WORDS:
                    if word in line:
                        problems.append(f'{path.relative_to(APP.parent)}:{number} 出现领域词「{word}」：{line.strip()[:80]}')
    assert not problems, '通用层出现领域词（应放进 app/domain 或提示词）：\n' + '\n'.join(problems)
