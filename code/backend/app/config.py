"""配置加载：config/*.yaml → 只读字典，带少量便捷访问。"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

BACKEND_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = Path(os.environ.get("DSZNT_CONFIG_DIR", BACKEND_DIR / "config"))


def _load(name: str) -> dict[str, Any]:
    with open(CONFIG_DIR / name, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


class Settings:
    def __init__(self, raw: dict[str, Any], rules: dict[str, Any], platforms: dict[str, Any]):
        self.raw = raw
        self.rules = rules
        self.platforms = platforms

    @property
    def adapter_mode(self) -> str:
        return os.environ.get("DSZNT_ADAPTER_MODE", self.raw.get("adapter_mode", "fake"))

    def path(self, key: str) -> Path:
        p = Path(self.raw["paths"][key])
        return p if p.is_absolute() else BACKEND_DIR / p

    @property
    def db_url(self) -> str:
        return os.environ.get("DSZNT_DB_URL", f"sqlite:///{self.path('db_file').as_posix()}")

    def model_for(self, role: str) -> dict[str, str]:
        return self.raw["models"][role]

    def price_for(self, model: str) -> dict[str, float]:
        return self.raw["model_prices"][model]

    @property
    def budget(self) -> dict[str, Any]:
        return self.raw["budget"]

    @property
    def agent_limits(self) -> dict[str, int]:
        return self.raw["agent_limits"]


@lru_cache
def get_settings() -> Settings:
    return Settings(_load("settings.yaml"), _load("rules.yaml"), _load("platforms.yaml"))
