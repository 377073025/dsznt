"""Anthropic Messages 格式的实现：Anthropic 本身，以及兼容这一格式的服务商（DeepSeek 等，传 base_url）。

API Key 从凭据管理器读取（见 app/secrets.py），不读文件。
"""
from __future__ import annotations

import json
from typing import Any

import anthropic
from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
from anthropic.types.messages.batch_create_params import Request

from app.adapters.llm.base import LLMRequest, LLMResult, Usage


def _schema_hint(schema: dict[str, Any]) -> str:
    return ("\n\n## 输出格式\n最终回答只输出一个 JSON 对象，不要输出其他文字，必须符合下面的 JSON Schema：\n"
            + json.dumps(schema, ensure_ascii=False))


def _params(req: LLMRequest, structured_output: bool = True) -> dict[str, Any]:
    system = req.system
    if req.output_schema and not structured_output:
        system += _schema_hint(req.output_schema)
    params: dict[str, Any] = {
        "model": req.model,
        "max_tokens": req.max_tokens,
        # 系统提示词固定在最前并标记缓存；工具定义顺序由注册表保证稳定
        "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
        "messages": req.messages,
    }
    if req.tools:
        params["tools"] = req.tools
    output_config: dict[str, Any] = {}
    if req.effort:
        output_config["effort"] = req.effort
    if req.output_schema and structured_output:
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
    def __init__(self, api_key: str, health_model: str, max_retries: int = 3, base_url: str | None = None,
                 structured_output: bool = True, supports_batch: bool = True):
        self.client = anthropic.Anthropic(api_key=api_key, max_retries=max_retries, base_url=base_url)
        self.health_model = health_model
        self.structured_output = structured_output
        self.supports_batch = supports_batch

    def run(self, req: LLMRequest) -> LLMResult:
        return _to_result(self.client.messages.create(**_params(req, self.structured_output)))

    def submit_batch(self, reqs: dict[str, LLMRequest]) -> str:
        if not self.supports_batch:
            raise RuntimeError("当前模型服务商不支持 Batch")
        batch = self.client.messages.batches.create(
            requests=[
                Request(custom_id=cid, params=MessageCreateParamsNonStreaming(**_params(r, self.structured_output)))
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
