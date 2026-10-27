"""智能体统一执行框架（07 号文档 7.1 节）。

组装输入 → 网关调模型（工具循环 ≤ max_turns）→ 结构化输出校验（失败重试 1 次）
→ 数字一致性校验（失败重试 1 次，仍失败交给 drop_invalid 丢弃不合格条目）→ 返回。
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Generic, TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from app.adapters.llm.base import LLMRequest, strict_json_schema
from app.agents.facts import FactTable, check_numbers
from app.agents.tracking import RunCancelled, RunHandle, RunTracker
from app.config import BACKEND_DIR, get_settings
from app.llm.gateway import Gateway
from app.services import Services
from app.tools.registry import ToolContext, ToolDef, tools_for

log = logging.getLogger(__name__)
Out = TypeVar("Out", bound=BaseModel)
PROMPTS_DIR = BACKEND_DIR / "app" / "prompts"


class AgentFailed(Exception):
    pass


@dataclass
class AgentSpec(Generic[Out]):
    name: str                      # 例如 ops.diagnose；前缀决定可用工具级别
    model_role: str                # 默认角色 ceo / main / light；可在 settings.yaml 的 agent_models 里按智能体覆盖
    output: type[Out]
    tool_names: list[str] = field(default_factory=list)
    max_turns: int | None = None
    # 数字校验不通过时，丢弃不合格条目后返回；None 表示整次失败
    drop_invalid: Callable[[Out, FactTable], Out] | None = None
    # 只校验这些文本（默认校验整份输出的 JSON 文本）
    numeric_text: Callable[[Out], str] | None = None

    @property
    def system_prompt(self) -> str:
        path = PROMPTS_DIR / f"{self.name}.md"
        return path.read_text(encoding="utf-8") if path.exists() else f"你是 {self.name}。"

    def tools(self) -> list[ToolDef]:
        return tools_for(self.name, self.tool_names)


_JSON_BLOCK = re.compile(r"\{.*\}", re.S)


def _parse(text: str, model: type[Out]) -> Out:
    m = _JSON_BLOCK.search(text)
    if not m:
        raise ValueError("输出里没有 JSON 对象")
    return model.model_validate(json.loads(m.group(0)))


class AgentRunner:
    """lane：main（主流程）/ adhoc（单独任务）。tracker 决定运行记录里的来源说明。"""

    def __init__(self, session: Session, services: Services, gateway: Gateway,
                 tracker: RunTracker | None = None, lane: str = "main"):
        self.s = session
        self.services = services
        self.gateway = gateway
        self.lane = lane
        self.tracker = tracker or RunTracker(source=lane)

    def run(
        self,
        spec: AgentSpec[Out],
        user_content: str | list[dict[str, Any]],
        facts: FactTable | None = None,
        context: dict[str, Any] | None = None,
        task_id: int | None = None,
    ) -> Out:
        model, effort = self.gateway.resolve(spec.name, spec.model_role)
        handle = self.tracker.open(spec.name, model)
        try:
            out = self._run(handle, spec, model, effort, user_content, facts, context, task_id)
        except RunCancelled as e:
            handle.finish("cancelled", str(e))
            raise
        except Exception as e:
            handle.finish("failed", f"{type(e).__name__}: {e}")
            raise
        handle.finish("succeeded")
        return out

    def _run(self, handle: RunHandle, spec: AgentSpec[Out], model: str, effort: str | None,
             user_content: str | list[dict[str, Any]], facts: FactTable | None,
             context: dict[str, Any] | None, task_id: int | None) -> Out:
        limits = get_settings().agent_limits
        max_turns = spec.max_turns or limits["max_turns"]
        tools = spec.tools()
        tool_map = {t.name: t for t in tools}
        ctx = ToolContext(self.s, self.services, spec.name, task_id, lane=self.lane)
        ctx_payload = {**(context or {}), "facts": facts.values if facts else {}}

        messages: list[dict[str, Any]] = [{"role": "user", "content": user_content}]
        used_tokens, format_retry, number_retry = 0, False, False

        for turn in range(max_turns):
            req = LLMRequest(
                agent=spec.name, model=model, effort=effort, system=spec.system_prompt, messages=messages,
                tools=[t.schema() for t in tools], output_schema=strict_json_schema(spec.output), context=ctx_payload,
                lane=self.lane,
            )
            handle.progress(f"等待模型（第 {turn + 1} 轮）")
            res = self.gateway.call(self.s, req)
            turn_tokens = res.usage.input_tokens + res.usage.output_tokens
            used_tokens += turn_tokens
            u = res.usage
            cost = self.gateway.cost_cny(res.model or model, u.input_tokens, u.output_tokens, u.cache_read_tokens, False)
            handle.turn_done(turn_tokens, round(cost * 100))
            if used_tokens > limits["max_tokens_per_run"]:
                raise AgentFailed(f"{spec.name} 超过单次 token 上限")
            if res.stop_reason == "refusal":
                raise AgentFailed(f"{spec.name} 模型拒绝回答")

            if res.tool_uses:
                messages.append({"role": "assistant", "content": res.content})
                results = []
                for b in res.tool_uses:
                    handle.progress(f"调用工具 {b['name']}")
                    r = self._run_tool(tool_map, ctx, b)
                    handle.step("tool", b["name"], f"参数 {json.dumps(b.get('input') or {}, ensure_ascii=False)} → {r['content']}")
                    results.append(r)
                messages.append({"role": "user", "content": results})
                continue

            try:
                out = _parse(res.text, spec.output)
            except (ValueError, ValidationError) as e:
                if format_retry:
                    raise AgentFailed(f"{spec.name} 输出格式两次不合格：{e}") from e
                format_retry = True
                handle.step("check", "格式校验重试", str(e), current="格式校验重试")
                messages += [{"role": "assistant", "content": res.content},
                             {"role": "user", "content": f"输出不符合要求的 JSON 结构：{e}。请只输出符合结构的 JSON。"}]
                continue

            if facts is None:
                return out
            text = spec.numeric_text(out) if spec.numeric_text else out.model_dump_json()
            bad = check_numbers(text, facts)
            if not bad:
                return out
            if not number_retry:
                number_retry = True
                handle.step("check", "数字校验重试", f"找不到出处的数字：{bad}", current="数字校验重试")
                messages += [{"role": "assistant", "content": res.content},
                             {"role": "user", "content": f"这些数字在输入数据里找不到出处：{bad}。只能使用输入里给出的数字，请重写。"}]
                continue
            if spec.drop_invalid:
                log.warning("%s 数字校验仍不通过，丢弃不合格条目：%s", spec.name, bad)
                return spec.drop_invalid(out, facts)
            raise AgentFailed(f"{spec.name} 数字校验两次不通过：{bad}")

        raise AgentFailed(f"{spec.name} 超过最大轮数 {max_turns}")

    def _run_tool(self, tool_map: dict[str, ToolDef], ctx: ToolContext, block: dict[str, Any]) -> dict[str, Any]:
        t = tool_map.get(block["name"])
        if t is None:
            return {"type": "tool_result", "tool_use_id": block["id"], "content": f"未注册的工具：{block['name']}", "is_error": True}
        try:
            content = t.invoke(ctx, block.get("input") or {})
            return {"type": "tool_result", "tool_use_id": block["id"], "content": content}
        except ValidationError as e:
            return {"type": "tool_result", "tool_use_id": block["id"], "content": f"参数不合法：{e}", "is_error": True}
        except Exception as e:  # noqa: BLE001  工具错误交还给模型，由它决定下一步
            log.warning("tool %s failed: %s", t.name, e)
            return {"type": "tool_result", "tool_use_id": block["id"], "content": f"{type(e).__name__}: {e}", "is_error": True}
