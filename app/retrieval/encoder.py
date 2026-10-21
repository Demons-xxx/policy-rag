"""BGE 文本编码器。

约定见 docs/架构与接口.md 3.7：
- CLS 池化 + L2 归一化，query 侧加检索前缀；
- 单条/多条都走 batch，max_length=512，cuda 下使用 half；
- 返回 float32 的 ``np.ndarray``，形状 ``(len(texts), dimension)``。
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
from transformers import AutoModel, AutoTokenizer

from app import config

# BGE 中文检索建议的 query 前缀，与 manifest.embedding.query_prefix 保持一致
QUERY_PREFIX = '为这个句子生成表示以用于检索相关文章：'
MAX_LENGTH = 512


class BgeEncoder:
    """BGE-base-zh-v1.5 编码器（CPU/GPU 自适应）。"""

    def __init__(self, model_dir: Path | None = None, device: str | None = None) -> None:
        self.model_dir = Path(model_dir) if model_dir else Path(config.EMBEDDING_DIR)
        if not self.model_dir.is_dir():
            raise FileNotFoundError(f'嵌入模型目录不存在：{self.model_dir}')
        self.device = str(device or config.resolve_device())
        self.tokenizer = AutoTokenizer.from_pretrained(str(self.model_dir))
        self.model = AutoModel.from_pretrained(str(self.model_dir))
        self.model.eval()
        self.model.to(self.device)
        if self.device == 'cuda':
            # half 只影响推理精度，输出统一转回 float32
            self.model.half()
        self.dimension = int(self.model.config.hidden_size)

    # ------------------------------------------------------------------
    # 编码
    # ------------------------------------------------------------------
    def encode(self, texts: list[str], query: bool = False,
               batch_size: int = 32) -> np.ndarray:
        """批量编码；``query=True`` 时自动加 BGE 检索前缀。"""
        items = [str(text if text is not None else '') for text in (texts or [])]
        if not items:
            return np.zeros((0, self.dimension), dtype=np.float32)
        if query:
            items = [QUERY_PREFIX + item for item in items]
        step = max(1, int(batch_size or 32))
        chunks: list[np.ndarray] = []
        for start in range(0, len(items), step):
            batch = items[start:start + step]
            inputs = self.tokenizer(
                batch,
                padding=True,
                truncation=True,
                max_length=MAX_LENGTH,
                return_tensors='pt',
            )
            inputs = {key: value.to(self.device) for key, value in inputs.items()}
            with torch.inference_mode():
                outputs = self.model(**inputs)
            # BGE 用 [CLS] 向量做句向量
            cls = outputs.last_hidden_state[:, 0]
            cls = torch.nn.functional.normalize(cls.float(), p=2, dim=1)
            chunks.append(cls.cpu().numpy().astype(np.float32))
        return np.vstack(chunks)

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------
    def count_tokens(self, text: str) -> int:
        """按 BGE 分词器计 token 数（证据预算用）。"""
        return len(self.tokenizer.encode(str(text or ''), add_special_tokens=False))

    def __repr__(self) -> str:  # pragma: no cover - 仅调试用
        return f'BgeEncoder(model_dir={self.model_dir!s}, device={self.device})'
