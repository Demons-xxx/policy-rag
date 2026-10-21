"""领域包：policy（政策/制度）。

领域包负责「这套 RAG 面向什么文档」：有哪些状态词、怎么算同一主题的
新旧版本、提示词怎么写。换一个垂直领域时，新增同结构的 domain 包，
而不是改检索/入库的通用代码。
"""
from app.domain import versions

__all__ = ['versions']
