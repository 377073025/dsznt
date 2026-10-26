"""模型服务适配器。消息格式沿用 Anthropic Messages API 的结构（dict），便于直接透传。"""
from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class LLMRequest(BaseModel):
    agent: str
    model: str
    effort: str | None = None
    system: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] = Field(default_factory=list)
    output_schema: dict[str, Any] | None = None
    max_tokens: int = 8000
    # 只给模拟实现看的上下文（不发给模型服务）
    context: dict[str, Any] = Field(default_factory=dict)


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0


class LLMResult(BaseModel):
    content: list[dict[str, Any]]
    stop_reason: str
    usage: Usage = Field(default_factory=Usage)
    model: str = ""

    @property
    def text(self) -> str:
        return "".join(b.get("text", "") for b in self.content if b.get("type") == "text")

    @property
    def tool_uses(self) -> list[dict[str, Any]]:
        return [b for b in self.content if b.get("type") == "tool_use"]


class LLMClient(Protocol):
    def run(self, req: LLMRequest) -> LLMResult: ...
    def submit_batch(self, reqs: dict[str, LLMRequest]) -> str: ...
    def batch_results(self, batch_id: str) -> dict[str, LLMResult] | None: ...  # None = 未完成


def strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    """pydantic 模型 → 结构化输出可用的 JSON Schema：对象全部字段必填、禁止多余字段、去掉默认值与标题。"""

    def fix(node: Any) -> Any:
        if isinstance(node, dict):
            node = {k: fix(v) for k, v in node.items() if k not in ("title", "default")}
            if node.get("type") == "object" and "properties" in node:
                node["additionalProperties"] = False
                node["required"] = list(node["properties"].keys())
            return node
        if isinstance(node, list):
            return [fix(x) for x in node]
        return node

    return fix(model.model_json_schema())
