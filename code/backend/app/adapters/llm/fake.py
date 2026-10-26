"""模拟模型：按智能体名称分发到「应答器」，应答器根据请求里的 context 生成确定性输出。

应答器只用 context 里已有的数字，所以能通过数字一致性校验；
测试里可以注册故意编数字的应答器，验证校验会拦截。
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app.adapters.llm.base import LLMRequest, LLMResult, Usage

Responder = Callable[[LLMRequest], "dict[str, Any] | str | LLMResult"]


def text_result(payload: dict[str, Any] | str) -> LLMResult:
    text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
    return LLMResult(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=800, output_tokens=200),
    )


def tool_call_result(name: str, tool_input: dict[str, Any], call_id: str = "toolu_fake_1") -> LLMResult:
    return LLMResult(
        content=[{"type": "tool_use", "id": call_id, "name": name, "input": tool_input}],
        stop_reason="tool_use",
        usage=Usage(input_tokens=600, output_tokens=60),
    )


class FakeLLM:
    def __init__(self, responders: dict[str, Responder] | None = None):
        from app.adapters.llm.fake_responders import DEFAULT_RESPONDERS

        self.responders: dict[str, Responder] = {**DEFAULT_RESPONDERS, **(responders or {})}
        self.requests: list[LLMRequest] = []
        self._batches: dict[str, dict[str, LLMRequest]] = {}

    def run(self, req: LLMRequest) -> LLMResult:
        self.requests.append(req)
        responder = self.responders.get(req.agent)
        if responder is None:
            return text_result({"error": f"no fake responder for {req.agent}"})
        out = responder(req)
        result = out if isinstance(out, LLMResult) else text_result(out)
        result.model = req.model
        return result

    def submit_batch(self, reqs: dict[str, LLMRequest]) -> str:
        bid = f"batch_fake_{len(self._batches) + 1}"
        self._batches[bid] = reqs
        return bid

    def batch_results(self, batch_id: str) -> dict[str, LLMResult] | None:
        return {cid: self.run(r) for cid, r in self._batches[batch_id].items()}

    def health_check(self) -> None:
        return None
