"""AI 认路（07 号文档 6.4 节）。

脚本某步失败 → 把页面无障碍树、任务目标、原脚本、失败原因交给模型 → 模型每次给一步白名单动作
→ 执行器校验并执行（与脚本同一套校验）→ 循环，最多 web_repair_max_steps 步
→ 拿到数据 → 整理成新版本脚本（version+1，updated_by=ai，state=candidate），连续 2 次成功后转正、旧脚本退役。
同一脚本 24 小时内认路超过 3 次 → 停止认路，交给开发者检查。
"""
from __future__ import annotations

import json
import logging
from datetime import timedelta
from typing import Any

from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRunner, AgentSpec
from app.config import get_settings
from app.data.channels.web_reader.script import Page, ScriptResult, ScriptRunner, run_script
from app.data.errors import FetchError, StepFailed
from app.db import utcnow
from app.models import WebScript

log = logging.getLogger(__name__)
PROMOTE_AFTER = 2
MAX_REPAIRS_PER_DAY = 3
SNAPSHOT_MAX_CHARS = 24000  # 约 8k token
_TARGET_ACTIONS = {"wait_for", "click", "read_text", "read_table"}
_VALUE_ACTIONS = {"set_date", "fill_search", "select_option"}


class RepairStep(BaseModel):
    done: bool
    action: str
    role: str
    name_contains: str
    value: str
    reason: str


SPEC = AgentSpec(name="web.repair", model_role="main", output=RepairStep, max_turns=2)


class RepairFailed(FetchError):
    pass


def active_script(s: Session, task_name: str) -> WebScript | None:
    """当前使用的脚本：有候选版本时先用候选（验证它），否则用正式版本。"""
    for state in ("candidate", "active"):
        w = s.scalar(select(WebScript).where(WebScript.task_name == task_name, WebScript.state == state)
                     .order_by(WebScript.script_version.desc()))
        if w is not None:
            return w
    return None


def run_task(s: Session, runner: AgentRunner, page: Page, task_name: str, platform: str, goal: str,
             params: dict[str, Any]) -> ScriptResult:
    script = active_script(s, task_name)
    if script is None:
        raise RepairFailed(f"没有可用的脚本：{task_name}")
    try:
        res = run_script(page, script.steps, platform, params)
    except StepFailed as e:
        if script.state == "candidate":
            # 候选脚本也失败了：作废，回到正式版本的基础上重新认路
            script.state = "retired"
        return _repair(s, runner, page, task_name, platform, goal, script.steps, e, params)
    script.success_streak += 1
    if script.state == "candidate" and script.success_streak >= PROMOTE_AFTER:
        for old in s.scalars(select(WebScript).where(WebScript.task_name == task_name, WebScript.state == "active")):
            old.state = "retired"
        script.state = "active"
    s.flush()
    return res


def _to_step(o: RepairStep) -> dict[str, Any]:
    target = {"role": o.role, "name_contains": o.name_contains}
    if o.action == "goto":
        return {"goto": o.value}
    if o.action in _VALUE_ACTIONS:
        return {o.action: {"target": target, "value": o.value}}
    if o.action == "read_table":
        return {"read_table": {"target": target}}
    if o.action in _TARGET_ACTIONS:
        return {o.action: target}
    return {o.action: o.value}  # 交给执行器按白名单拒绝


def _repair(s: Session, runner: AgentRunner, page: Page, task_name: str, platform: str, goal: str,
            old_steps: list[dict[str, Any]], error: StepFailed, params: dict[str, Any]) -> ScriptResult:
    since = utcnow() - timedelta(hours=24)
    recent = s.scalar(select(func.count(WebScript.id)).where(
        WebScript.task_name == task_name, WebScript.updated_by == "ai", WebScript.created_at >= since)) or 0
    if recent >= MAX_REPAIRS_PER_DAY:
        raise RepairFailed(f"{task_name} 24 小时内已认路 {recent} 次，停止自动认路，请开发者检查")

    max_steps = get_settings().agent_limits["web_repair_max_steps"]
    exe = ScriptRunner(page, platform, params)
    history: list[dict[str, Any]] = []
    new_steps: list[dict[str, Any]] = []
    for _ in range(max_steps):
        snapshot = json.dumps(page.snapshot(), ensure_ascii=False)[:SNAPSHOT_MAX_CHARS]
        content = (
            f"任务目标：{goal}\n原脚本：{json.dumps(old_steps, ensure_ascii=False)}\n"
            f"失败：第 {error.step_index + 1} 步，{error}\n当前地址：{page.url}\n"
            f"已执行：{json.dumps(history, ensure_ascii=False)}\n页面无障碍树：{snapshot}"
        )
        out = runner.run(SPEC, content, context={"history": history, "goal": goal, "snapshot": page.snapshot()})
        if out.done:
            break
        step = _to_step(out)
        try:
            exe.run_step(step)  # ForbiddenAction / 风控异常直接抛出，终止认路
        except StepFailed as e:
            history.append({"step": step, "ok": False, "error": str(e)})
            continue
        history.append({"step": step, "ok": True})
        new_steps.append(step)

    if not exe.result.rows:
        raise RepairFailed(f"{task_name} 认路 {len(history)} 步后仍未读到数据")
    mapping = [st for st in old_steps if "map_columns" in st]
    for st in mapping:
        exe.run_step(st)
    entry = [st for st in old_steps if "goto" in st][:1]
    version = (s.scalar(select(func.max(WebScript.script_version)).where(WebScript.task_name == task_name)) or 0) + 1
    s.add(WebScript(task_name=task_name, script_version=version, steps=entry + new_steps + mapping,
                    updated_by="ai", state="candidate", success_streak=0))
    s.flush()
    log.info("web.repair %s → v%s（%s 步）", task_name, version, len(new_steps))
    return exe.result
