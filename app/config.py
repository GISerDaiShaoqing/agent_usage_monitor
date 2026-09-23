"""配置管理：config.json 读写 + OpenCode Go API key 自动发现。"""
from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
CONFIG_PATH = APP_DIR / "config.json"
STORE_PATH = APP_DIR / "data" / "snapshots.db"

USER_HOME = Path.home()

# 默认换肤壁纸候选（留空 = 新用户默认不开换肤；在自己的 config.json 里填 background_image 即可启用）
DEFAULT_BG_CANDIDATES: list[Path] = []

# metrik 同款回退顺序：环境变量 > ZCode provider 配置 > OpenCode auth.json
ZCODE_PROVIDER_CONFIG = USER_HOME / ".zcode" / "v2" / "provider_config.json"
OPENCODE_AUTH_CANDIDATES = [
    USER_HOME / ".local" / "share" / "opencode" / "auth.json",
    Path(os_appdata := USER_HOME / "AppData" / "Roaming") / "opencode" / "auth.json",
]

ZCODE_DB_CANDIDATES = [
    USER_HOME / ".zcode" / "cli" / "db" / "db.sqlite",
]
OPENCODE_DB_CANDIDATES = [
    USER_HOME / ".local" / "share" / "opencode" / "opencode.db",
    (os_appdata) / "opencode" / "opencode.db",
]


@dataclass
class Config:
    api_key: str = ""                # 留空则自动发现
    poll_interval: int = 60          # 额度轮询秒数
    refresh_interval: int = 30       # UI 刷新秒数
    provider_filter: str = ""        # 空为当前套餐默认；"*"全部；其它=provider 前缀
    active_plan: str = "opencode-go"  # 当前套餐（s 键/设置页切换）
    background_image: str = ""       # 换肤背景图路径（空=不换肤）
    background_dim: float = 0.35     # 背景压暗程度 0-1（越小越亮）
    widget_width: int = 340          # 悬浮小部件宽(px)
    widget_height: int = 210         # 悬浮小部件高(px)
    widget_opacity: float = 0.92     # 小部件整体不透明度 0-1
    widget_topmost: bool = True      # 小部件置顶
    discovered_key: str = field(default="", repr=False)
    discovered_from: str = ""

    def save(self) -> None:
        CONFIG_PATH.write_text(
            json.dumps({k: v for k, v in asdict(self).items()
                        if k not in ("discovered_key", "discovered_from")},
                       ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def load_config() -> Config:
    cfg = Config()
    if CONFIG_PATH.exists():
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for f in ("api_key", "poll_interval", "refresh_interval",
                      "provider_filter", "active_plan", "background_image",
                      "background_dim", "widget_width", "widget_height",
                      "widget_opacity", "widget_topmost"):
                if f in data:
                    setattr(cfg, f, data[f])
        except (json.JSONDecodeError, OSError):
            pass
    else:
        # 首次生成：配置好候选壁纸则直接启用换肤
        if DEFAULT_BG_CANDIDATES:
            cfg.background_image = str(DEFAULT_BG_CANDIDATES[0])
        cfg.save()
    cfg.discovered_key, cfg.discovered_from = discover_api_key(cfg.api_key)
    return cfg


def discover_api_key(explicit: str = "") -> tuple[str, str]:
    """返回 (key, 来源描述)。explicit 非空时直接用。"""
    if explicit:
        return explicit, "手动配置"
    import os
    env = os.environ.get("OPENCODE_GO_API_KEY")
    if env:
        return env, "环境变量 OPENCODE_GO_API_KEY"
    for path, getter in _credential_sources():
        try:
            key = getter(path)
            if key:
                return key, str(path)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            continue
    return "", ""


def _credential_sources():
    def from_zcode(path: Path) -> str:
        data = json.loads(path.read_text(encoding="utf-8"))
        # config.providerConfigRules.providerRules[] 中
        # providerId 以 opencode-go 开头的条目
        rules_cfg = data.get("config", {}).get("providerConfigRules", {})
        rules = (rules_cfg.get("providerRules", [])
                 if isinstance(rules_cfg, dict) else rules_cfg)
        for rule in rules or []:
            if not isinstance(rule, dict):
                continue
            pid = rule.get("providerId", "")
            if pid.startswith("opencode-go"):
                access = rule.get("config", {}).get("access", {})
                if access.get("type") == "api-key" and access.get("apiKey"):
                    return access["apiKey"]
        return ""

    def from_opencode_auth(path: Path) -> str:
        data = json.loads(path.read_text(encoding="utf-8"))
        entry = data.get("opencode-go") or {}
        if entry.get("type") == "api_key" and entry.get("key"):
            return entry["key"]
        return ""

    yield ZCODE_PROVIDER_CONFIG, from_zcode
    for p in OPENCODE_AUTH_CANDIDATES:
        yield p, from_opencode_auth


ZCODE_V2_CONFIG = USER_HOME / ".zcode" / "v2" / "config.json"
OPENCODE_GATEWAY_MARK = "opencode.ai/zen/go"


def discover_provider_ids() -> tuple[str, ...]:
    """发现指向 OpenCode Go 网关的 provider_id 集合。

    1) provider_config.json 中 providerId 以 opencode-go 开头的
    2) v2/config.json 自定义 provider 中 baseURL 含 opencode.ai/zen/go 的（含 UUID id）
    """
    ids: list[str] = []
    try:
        data = json.loads(ZCODE_PROVIDER_CONFIG.read_text(encoding="utf-8"))
        rules_cfg = data.get("config", {}).get("providerConfigRules", {})
        rules = (rules_cfg.get("providerRules", [])
                 if isinstance(rules_cfg, dict) else rules_cfg)
        for rule in rules or []:
            if isinstance(rule, dict) and rule.get("providerId", "").startswith("opencode-go"):
                ids.append(rule["providerId"])
    except (OSError, ValueError):
        pass
    try:
        v2 = json.loads(ZCODE_V2_CONFIG.read_text(encoding="utf-8"))
        for pid, entry in (v2.get("provider") or {}).items():
            base = str((entry or {}).get("options", {}).get("baseURL", ""))
            if OPENCODE_GATEWAY_MARK in base:
                ids.append(str(pid))
    except (OSError, ValueError):
        pass
    return tuple(dict.fromkeys(ids)) or ("opencode-go",)


def find_db() -> tuple[Path | None, Path | None]:
    """返回 (zcode_db, opencode_db) 中存在的第一个路径。"""
    def first(paths: list[Path]) -> Path | None:
        for p in paths:
            if p.exists():
                return p
        return None
    return first(ZCODE_DB_CANDIDATES), first(OPENCODE_DB_CANDIDATES)
