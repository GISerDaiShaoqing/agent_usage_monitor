"""模型单价表：每百万 token 美元单价，用于估算花费。

单价来自 OpenCode Go 官方文档（https://opencode.ai/docs/go）。
匹配规则：先精确名，再小写归一化后精确匹配；无价模型返回 None（不瞎估）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PRICES_PATH = Path(__file__).resolve().parent / "prices.json"

# 内置兜底表（prices.json 可覆盖/扩充）。格式: model_id 小写 -> (input, output, cache_read)
# 单价均为 USD / 1M tokens。cache_read 无官方价时按 input 的 10% 近似并标注。
BUILTIN_PRICES: dict[str, dict] = {
    "glm-5.3-flash": {"input": 0.15, "output": 0.50, "cache_read": 0.02},
    "glm-5.3":       {"input": 0.60, "output": 2.00, "cache_read": 0.06},
    "glm-5.2":       {"input": 0.60, "output": 2.00, "cache_read": 0.06},
    "glm-5.1":       {"input": 0.60, "output": 2.00, "cache_read": 0.06},
    "kimi-k3":       {"input": 0.60, "output": 2.50, "cache_read": 0.06},
    "kimi-k2.7-code": {"input": 0.45, "output": 1.80, "cache_read": 0.05},
    "kimi-k2.6":     {"input": 0.45, "output": 1.80, "cache_read": 0.05},
    "deepseek-v4.1-flash": {"input": 0.28, "output": 1.12, "cache_read": 0.028},
    "deepseek-v4-flash":   {"input": 0.28, "output": 1.12, "cache_read": 0.028},
    "deepseek-v4-pro":     {"input": 0.60, "output": 2.40, "cache_read": 0.06},
    "mimo-v2.5":           {"input": 0.30, "output": 1.20, "cache_read": 0.03},
    "mimo-v2.5-pro":       {"input": 0.60, "output": 2.40, "cache_read": 0.06},
    "minimax-m3":          {"input": 0.40, "output": 1.60, "cache_read": 0.04},
    "qwen3.8-max":         {"input": 0.60, "output": 2.40, "cache_read": 0.06},
    "qwen3.8-flash":       {"input": 0.15, "output": 0.60, "cache_read": 0.015},
    "gpt-5.6-luna":        {"input": 1.25, "output": 10.00, "cache_read": 0.125},
    "grok-4.6":            {"input": 3.00, "output": 15.00, "cache_read": 0.30},
}


@dataclass
class ModelPrice:
    input: float
    output: float
    cache_read: float


def _normalize(model_id: str) -> str:
    """归一化模型名：小写、去掉 provider 前缀（如 opencode-go/）。"""
    name = model_id.strip().lower()
    if "/" in name:
        name = name.rsplit("/", 1)[-1]
    return name


class Pricing:
    def __init__(self) -> None:
        table = dict(BUILTIN_PRICES)
        if PRICES_PATH.exists():
            try:
                table.update(json.loads(PRICES_PATH.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                pass
        self._table = {k: ModelPrice(**v) for k, v in table.items()}

    def get(self, model_id: str) -> ModelPrice | None:
        return self._table.get(_normalize(model_id))

    def cost(self, model_id: str, *, input_uncached: int, output: int,
             cache_read: int, cache_write: int = 0) -> float | None:
        """估算一次调用的美元花费；无价模型返回 None。"""
        p = self.get(model_id)
        if p is None:
            return None
        return (input_uncached / 1e6 * p.input
                + output / 1e6 * p.output
                + cache_read / 1e6 * p.cache_read
                + cache_write / 1e6 * p.input)


def normalize_model(model_id: str) -> str:
    return _normalize(model_id)
