"""模型单价表：每百万 token 美元单价，用于估算花费。

单价来自 OpenCode Go 官方文档（https://opencode.ai/docs/go），2026-10-30 核对。
官方部分模型按上下文长度分档 / 深度思考分时段（DeepSeek 峰谷）计价；
逐请求上下文不可知，统一按低档（≤ 阈值档）与谷段价估算，注释中标明依据。
无价模型返回 None（不瞎估）。app/prices.json 可覆盖本表。
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

PRICES_PATH = Path(__file__).resolve().parent / "prices.json"

# 内置兜底表（prices.json 可覆盖/扩充）。格式: model_id 小写 ->
# {"input": …, "output": …, "cache_read": …, "cache_write": 可选}
# 单价均为 USD / 1M tokens；分档模型取低档价（GPT6 Luna ≤272K /
# Grok 4.7 ≤200K / MiniMax M3 ≤512K / Qwen3.7+ ≤256K / Haiku ≤100K）；
# DeepSeek 取谷段（峰段=谷×2，周中 01:00–04:00、06:00–10:00 UTC）。
BUILTIN_PRICES: dict[str, dict] = {
    # ---- GLM ----
    "glm-5.3-flash": {"input": 0.15, "output": 0.50, "cache_read": 0.03},
    "glm-5.3":       {"input": 1.40, "output": 4.40, "cache_read": 0.26},
    "glm-5.2":       {"input": 1.40, "output": 4.40, "cache_read": 0.26},
    "glm-5.1":       {"input": 0.60, "output": 2.00, "cache_read": 0.06},  # 旧表值（新文档未列）
    # ---- Kimi ----
    "kimi-k3":        {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.00},
    "kimi-k2.7-code": {"input": 0.95, "output": 4.00, "cache_read": 0.19},
    "kimi-k2.6":      {"input": 0.45, "output": 1.80, "cache_read": 0.05},  # 旧表值（新文档未列）
    # ---- DeepSeek（谷段）----
    "deepseek-v4.1-flash": {"input": 0.15, "output": 0.60, "cache_read": 0.003},
    "deepseek-v4-flash":   {"input": 0.15, "output": 0.60, "cache_read": 0.003},
    "deepseek-v4-pro":     {"input": 0.66, "output": 1.98, "cache_read": 0.022},
    # ---- MiMo ----
    "mimo-v2.5":       {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
    "mimo-v2.5-pro":   {"input": 0.435, "output": 0.87, "cache_read": 0.003625},
    "mimo-v2.6-flash": {"input": 0.14, "output": 0.28, "cache_read": 0.0028},
    "mimo-v2.6-pro":   {"input": 0.435, "output": 0.87, "cache_read": 0.003625},
    # ---- MiniMax ----
    "minimax-m3":            {"input": 0.30, "output": 1.20, "cache_read": 0.06},
    "minimax-m3.1-flash-free": {"input": 0.0, "output": 0.0, "cache_read": 0.0},
    # ---- Qwen ----
    "qwen3.8-max":   {"input": 2.00, "output": 6.00, "cache_read": 0.25, "cache_write": 2.50},
    "qwen3.8-flash": {"input": 0.15, "output": 0.47, "cache_read": 0.016, "cache_write": 0.20},
    "qwen3.7-plus":  {"input": 0.40, "output": 1.60, "cache_read": 0.04, "cache_write": 0.50},
    # ---- GPT / xAI（openai/v1 responses 组；luna 系列低档）----
    "gpt-6-luna":   {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "gpt-5.6-luna": {"input": 0.20, "output": 1.20, "cache_read": 0.02, "cache_write": 0.25},
    "grok-4.7":     {"input": 2.00, "output": 6.00, "cache_read": 0.50},
    "grok-4.6":     {"input": 3.00, "output": 15.00, "cache_read": 0.30},   # 旧表值（新文档未列）
    # ---- Anthropic / Google / 其它 ----
    "claude-haiku-5-5":         {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125},
    "gemini-4-argon":           {"input": 2.00, "output": 10.00, "cache_read": 0.10},  # 限时 5 折至 2026-11-30
    "step-5-preview":           {"input": 1.00, "output": 2.70, "cache_read": 0.10},
    "hy4-preview":              {"input": 0.834, "output": 2.501, "cache_read": 0.042},
    "hy3":                      {"input": 0.14, "output": 0.58, "cache_read": 0.035},
    "longcat-2.0":              {"input": 0.30, "output": 1.20, "cache_read": 0.006},
    "muse-spark-1.2-contributor": {"input": 0.10, "output": 0.20, "cache_read": 0.002},
    "muse-spark-1.3-contributor": {"input": 0.10, "output": 0.20, "cache_read": 0.002},
}


@dataclass
class ModelPrice:
    input: float
    output: float
    cache_read: float
    cache_write: float | None = None   # 缓存写入单价；缺省按 input 价计


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
        self._table = {}
        for k, v in table.items():
            if not isinstance(v, dict):
                continue          # 蹩脚的 prices.json 行不炸，忽略
            self._table[k] = ModelPrice(
                input=v["input"], output=v["output"],
                cache_read=v.get("cache_read", 0.0),
                cache_write=v.get("cache_write"))

    def get(self, model_id: str) -> ModelPrice | None:
        return self._table.get(_normalize(model_id))

    def cost(self, model_id: str, *, input_uncached: int, output: int,
             cache_read: int, cache_write: int = 0) -> float | None:
        """估算一次调用的美元花费；无价模型返回 None。"""
        p = self.get(model_id)
        if p is None:
            return None
        write_price = p.cache_write if p.cache_write is not None else p.input
        return (input_uncached / 1e6 * p.input
                + output / 1e6 * p.output
                + cache_read / 1e6 * p.cache_read
                + cache_write / 1e6 * write_price)


def normalize_model(model_id: str) -> str:
    return _normalize(model_id)
