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
    def llm(self) -> dict[str, Any]:
        """模型服务商配置；没写时按 Anthropic 默认。"""
        return {"provider": "anthropic", "key_ref": "anthropic_api_key", "base_url": None,
                "structured_output": True, "batch": True, **(self.raw.get("llm") or {})}

    @property
    def db_url(self) -> str:
        return os.environ.get("DSZNT_DB_URL", f"sqlite:///{self.path('db_file').as_posix()}")

    def model_for(self, role: str) -> dict[str, str]:
        return self.raw["models"][role]

    def model_for_agent(self, agent: str, default_role: str) -> dict[str, str]:
        """智能体实际使用的模型。查找顺序：agent_models 里的完整名（ceo.daily）> 智能体名（ceo）> 代码里的默认角色。

        配置值可以是角色名（"main"），也可以是 {role?, model?, effort?}：先取角色的模型，再用 model / effort 覆盖。
        """
        table = self.raw.get("agent_models") or {}
        for key in (agent, agent.split(".", 1)[0]):
            if key not in table:
                continue
            v = table[key]
            if isinstance(v, str):
                return self.model_for(v)
            base = self.model_for(v.get("role", default_role))
            return {**base, **{k: v[k] for k in ("model", "effort") if k in v}}
        return self.model_for(default_role)

    def validate_models(self) -> None:
        """启动时检查模型配置：引用的角色必须存在，用到的模型必须有价格（否则无法计量成本）。"""
        roles, prices = self.raw["models"], self.raw["model_prices"]
        errors = [f"models.{r} 的模型 {m['model']} 没有价格" for r, m in roles.items() if m["model"] not in prices]
        for key, v in (self.raw.get("agent_models") or {}).items():
            role = v if isinstance(v, str) else v.get("role")
            if role is not None and role not in roles:
                errors.append(f"agent_models.{key} 引用了不存在的角色 {role}")
            elif isinstance(v, dict) and v.get("model") and v["model"] not in prices:
                errors.append(f"agent_models.{key} 的模型 {v['model']} 没有价格")
        if errors:
            raise ValueError("模型配置有误：" + "；".join(errors))

    def price_for(self, model: str) -> dict[str, float]:
        return self.raw["model_prices"][model]

    @property
    def budget(self) -> dict[str, Any]:
        return self.raw["budget"]

    @property
    def adhoc(self) -> dict[str, Any]:
        return self.raw["adhoc"]

    @property
    def agent_limits(self) -> dict[str, int]:
        return self.raw["agent_limits"]


@lru_cache
def get_settings() -> Settings:
    settings = Settings(_load("settings.yaml"), _load("rules.yaml"), _load("platforms.yaml"))
    settings.validate_models()
    return settings
