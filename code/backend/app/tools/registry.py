"""工具注册表（07 号文档 7.2 节）。智能体只能调用注册给它的工具，这是它碰外部的唯一入口。

工具分三级：
- readonly：只读，分析、选品类智能体可用
- internal：写系统内部（任务表、素材库、物料区），CEO 与执行 Agent 可用
- shop：写店铺，只有执行 Agent 可用，且内部再校验任务状态与写入确认
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.adapters.llm.base import strict_json_schema
from app.services import Services

Level = Literal["readonly", "internal", "shop"]


@dataclass
class ToolContext:
    session: Session
    services: Services
    agent: str
    task_id: int | None = None


@dataclass(frozen=True)
class ToolDef:
    name: str
    description: str
    params: type[BaseModel]
    fn: Callable[[ToolContext, Any], Any]
    level: Level

    def schema(self) -> dict[str, Any]:
        return {"name": self.name, "description": self.description, "input_schema": strict_json_schema(self.params)}

    def invoke(self, ctx: ToolContext, raw_input: dict[str, Any]) -> str:
        args = self.params.model_validate(raw_input)
        out = self.fn(ctx, args)
        if isinstance(out, BaseModel):
            out = out.model_dump(mode="json")
        return out if isinstance(out, str) else json.dumps(out, ensure_ascii=False, default=str)


TOOLS: dict[str, ToolDef] = {}


def tool(name: str, description: str, params: type[BaseModel], level: Level):
    def deco(fn: Callable[[ToolContext, Any], Any]) -> Callable[[ToolContext, Any], Any]:
        if name in TOOLS:
            raise ValueError(f"duplicate tool {name}")
        TOOLS[name] = ToolDef(name, description, params, fn, level)
        return fn

    return deco


# 各类智能体允许的最高工具级别
AGENT_MAX_LEVEL: dict[str, Level] = {
    "selection": "readonly",
    "ops": "readonly",
    "web": "readonly",
    "ceo": "internal",
    "exec": "shop",
}
_ORDER = {"readonly": 0, "internal": 1, "shop": 2}


def tools_for(agent: str, names: list[str]) -> list[ToolDef]:
    """按名称取工具，并检查不超过该类智能体允许的级别。"""
    import app.tools.readonly  # noqa: F401  注册工具
    import app.tools.write  # noqa: F401

    family = agent.split(".", 1)[0]
    max_level = AGENT_MAX_LEVEL[family]
    out = []
    for n in names:
        t = TOOLS[n]
        if _ORDER[t.level] > _ORDER[max_level]:
            raise PermissionError(f"{agent} 不能注册 {t.level} 级工具 {n}")
        out.append(t)
    return out
