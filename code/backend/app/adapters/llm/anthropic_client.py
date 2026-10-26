"""Anthropic 实现。API Key 从凭据管理器读取（见 app/secrets.py），不读文件。"""
from __future__ import annotations

from typing import Any

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from app.adapters.llm.base import LLMRequest, LLMResult, Usage


def _params(req: LLMRequest) -> dict[str, Any]:
    params: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        # 系统提示词固定在最前并标记缓存；工具定义顺序由注册表保证稳定
        "system": [{"type": "text", "text": req.system, "cache_control": {"type": "ephemeral"}}],
        "messages": req.messages,
    }
    if req.tools:
        params["tools"] = req.tools
    output_config: dict[str, Any] = {}
    if req.effort:
        output_config["effort"] = req.effort
    if req.output_schema:
        output_config["format"] = {"type": "json_schema", "schema": req.output_schema}
    if output_config:
        params["output_config"] = output_config
    return params


def _to_result(msg: Any) -> LLMResult:
    u = msg.usage
    return LLMResult(
        content=[b.to_dict() for b in msg.content],
        stop_reason=msg.stop_reason or "",
        model=msg.model,
        usage=Usage(
            input_tokens=u.input_tokens or 0,
            output_tokens=u.output_tokens or 0,
            cache_read_tokens=getattr(u, "cache_read_input_tokens", 0) or 0,
        ),
    )


class AnthropicLLM:
    def __init__(self, api_key: str, health_model: str, max_retries: int = 3):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries)
        self.health_model = health_model

    def run(self, req: LLMRequest) -> LLMResult:
        return _to_result(self.client.messages.create(**_params(req)))

    def submit_batch(self, reqs: dict[str, LLMRequest]) -> str:
        batch = self.client.messages.batches.create(
            requests=[
                Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**_params(r)))
                for cid, r in reqs.items()
            ]
        )
        return batch.id

    def batch_results(self, batch_id: str) -> dict[str, LLMResult] | None:
        batch = self.client.messages.batches.retrieve(batch_id)
        if batch.processing_status != "ended":
            return None
        out: dict[str, LLMResult] = {}
        for r in self.client.messages.batches.results(batch_id):
            if r.result.type == "succeeded":
                out[r.custom_id] = _to_result(r.result.message)
            else:
                # 失败 / 取消 / 过期的请求由调用方改走实时调用
                out[r.custom_id] = LLMResult(content=[], stop_reason=f"batch_{r.result.type}")
        return out

    def health_check(self) -> None:
        self.client.messages.create(
            model=self.health_model, max_tokens=16, messages=[{"role": "user", "content": "ping"}]
        )
