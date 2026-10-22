"""交叉编码器重排（bge-reranker-v2-m3）。

检索流水线在 RRF 融合与硬过滤之后、多样性选择之前，用本模块对前 N 个候选
按「问题-条款」相关性重排。模型缺失、加载失败或关闭开关时一律回退到原排序，
不影响问答可用性。

设计取舍：
- 只重排前 ``RERANK_TOP_N``（默认 20）个候选，控制延迟；
- CUDA 下用 half，批量编码，20 条条款约 60–120ms；
- 进程内单例，首次调用才加载模型；失败后记住状态，不再反复重试。
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path

from app import config

logger = logging.getLogger(__name__)

MAX_LENGTH = 512
_lock = threading.Lock()
_cache: dict[str, object] = {'reranker': None, 'failed': False}


class BgeReranker:
    """bge-reranker-v2-m3：``score(question, texts)`` 返回越大越相关的分数。"""

    def __init__(self, model_dir: Path | None = None, device: str | None = None) -> None:
        import torch
        from transformers import AutoModelForSequenceClassification, AutoTokenizer

        self.model_dir = Path(model_dir) if model_dir else Path(config.RERANK_MODEL_DIR)
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f'重排模型目录不存在：{self.model_dir}')
        self.device = str(device or config.resolve_device())
        self.tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        self.model = AutoModelForSequenceClassification.from_pretrained(
            str(self.model_dir), num_labels=1)
        self.model.eval()
        self.model.to(self.device)
        if self.device == 'cuda':
            self.model.half()

    def score(self, question: str, texts: list[str],
              batch_size: int | None = None) -> list[float]:
        """对一批候选文本打分；空输入返回空列表。"""
        import torch

        items = [str(text or '') for text in (texts or [])]
        if not items:
            return []
        from app.retrieval import gpu_guard

        with gpu_guard.gpu_slot('重排'):
            return self._score_locked(question, items, batch_size)

    def _score_locked(self, question: str, items: list[str],
                      batch_size: int | None = None) -> list[float]:
        import torch

        query = str(question or '')
        step = max(1, int(batch_size or config.RERANK_BATCH or 16))
        scores: list[float] = []
        for start in range(0, len(items), step):
            batch = [[query, item] for item in items[start:start + step]]
            inputs = self.tokenizer(batch, padding=True, truncation=True,
                                    max_length=MAX_LENGTH, return_tensors='pt')
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with torch.inference_mode():
                logits = self.model(**inputs).logits
            scores.extend(float(v) for v in logits.view(-1).float().cpu().tolist())
        return scores


def get_reranker() -> BgeReranker | None:
    """获取进程内单例；不可用时返回 None（只尝试加载一次）。"""
    if not getattr(config, 'RERANK_ENABLED', False):
        return None
    with _lock:
        if _cache['reranker'] is not None:
            return _cache['reranker']  # type: ignore[return-value]
        if _cache['failed']:
            return None
        try:
            reranker = BgeReranker()
        except Exception as exc:  # 模型缺失/依赖缺失都不应影响问答
            _cache['failed'] = True
            logger.warning('重排模型加载失败，已回退原排序：%s', exc)
            return None
        _cache['reranker'] = reranker
        logger.info('重排模型已加载：%s (%s)', reranker.model_dir, reranker.device)
        return reranker


def reset_reranker_cache() -> None:
    """测试用：清空单例状态。"""
    with _lock:
        _cache['reranker'] = None
        _cache['failed'] = False


__all__ = ['BgeReranker', 'get_reranker', 'reset_reranker_cache']
