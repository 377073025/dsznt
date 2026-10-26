"""CEO Agent：汇总早报、派活、验收。读全部、写任务表，不直接写店铺。"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.ops import build_input as ops_input  # 早报复用运营 Agent 的事实表
from app.agents.runtime import AgentRunner, AgentSpec
from app.data import queries
from app.models import Acceptance, Report, Suggestion, Task
from app.rules.metrics import classify, compare
from app.workflow.state_machine import Actor, transition

CEO = Actor("agent", "ceo")


# ---------- 早报 ----------

class DailyReportOut(BaseModel):
    headline: str
    best: str
    normal: str
    dropping: str
    suggestion_ids: list[int]  # 进入早报的建议，按优先级排序
    data_status: str


DAILY = AgentSpec(
    name="ceo.daily", model_role="ceo", output=DailyReportOut,
    numeric_text=lambda o: " ".join([o.headline, o.best, o.normal, o.dropping]),
)


def daily_report(runner: AgentRunner, s: Session, day: date, data_status: str, reviews: list[str] | None = None) -> Report:
    """reviews：程序生成的复盘结论，原样放进早报，不经过模型改写。"""
    _, facts, _ = ops_input(s, day)
    rows = queries.day_rows(s, day, 14)
    names = {p.goods_id: p.name for p in queries.products(s)}
    groups = classify([compare(rows, g, day) for g in names])
    sugs = s.scalars(select(Suggestion).where(Suggestion.day == day, Suggestion.status == "generated")).all()
    facts.ignore_strings.update(names.values())
    for x in sugs:
        facts.add(f"suggestion_{x.id}.id", x.id)
    content = (
        f"日期 {day.isoformat()}。请汇总店铺早报。\n"
        f"分组（程序计算）：最好卖 {[names[g] for g in groups['best']]}；掉在哪 {[names[g] for g in groups['dropping']]}；"
        f"正常 {[names[g] for g in groups['normal']]}\n"
        f"数据状态：{data_status}\n"
        "可用数字：\n" + facts.as_prompt_lines() + "\n\n运营建议：\n"
        + "\n".join(f"#{x.id} [{x.confidence}] {x.action}，{x.reason}" for x in sugs)
    )
    ctx = {"groups": {k: [names[g] for g in v] for k, v in groups.items()},
           "suggestions": [{"id": x.id, "action": x.action} for x in sugs], "data_status": data_status}
    out = runner.run(DAILY, content, facts=facts, context=ctx)

    valid = {x.id for x in sugs}
    chosen = [i for i in out.suggestion_ids if i in valid]
    for x in sugs:
        transition(s, "suggestion", x.id, "report" if x.id in chosen else "discard", CEO)
    report = s.scalar(select(Report).where(Report.kind == "daily", Report.period == day.isoformat()))
    if report is None:
        report = Report(kind="daily", period=day.isoformat())
        s.add(report)
    report.content = {**out.model_dump(), "suggestion_ids": chosen, "reviews": reviews or []}
    s.flush()
    return report


# ---------- 派活 ----------

class DispatchOut(BaseModel):
    task_ids: list[int]
    note: str


DISPATCH = AgentSpec(name="ceo.dispatch", model_role="ceo", output=DispatchOut, tool_names=["create_task"])


def dispatch(runner: AgentRunner, s: Session, suggestion_id: int) -> list[Task]:
    sug = s.get(Suggestion, suggestion_id)
    assert sug is not None and sug.status == "approved"
    s.info["dispatch_ref"] = ("suggestion", sug.id)
    try:
        content = (
            f"店主已同意以下建议，请拆成执行任务（用 create_task），每个任务写明验收清单。\n"
            f"建议 #{sug.id}：{sug.action}，{sug.reason}\n商品：{sug.goods_id}；类型：{sug.type}；需要写店铺：{sug.needs_write}"
        )
        ctx = {"suggestion": {"id": sug.id, "goods_id": sug.goods_id, "type": sug.type, "needs_write": sug.needs_write}}
        out = runner.run(DISPATCH, content, context=ctx)
    finally:
        s.info.pop("dispatch_ref", None)
    tasks = list(s.scalars(select(Task).where(Task.ref_type == "suggestion", Task.ref_id == sug.id)).all())
    if not tasks:
        raise RuntimeError(f"建议 #{sug.id} 派活后没有生成任务：{out.note}")
    transition(s, "suggestion", sug.id, "dispatch", CEO, {"tasks": [t.id for t in tasks]})
    return tasks


# ---------- 验收 ----------

class CheckItem(BaseModel):
    item: str
    ok: bool
    note: str


class AcceptOut(BaseModel):
    passed: bool
    items: list[CheckItem]
    comment: str


ACCEPT = AgentSpec(name="ceo.accept", model_role="ceo", output=AcceptOut, tool_names=["get_task_output"])
MAX_REWORK = 2


def accept(runner: AgentRunner, s: Session, task_id: int) -> Acceptance:
    task = s.get(Task, task_id)
    assert task is not None and task.status == "submitted"
    content = f"请验收任务 #{task.id}（用 get_task_output 查看产物），逐条对照验收清单：{task.checklist}"
    ctx = {"task_id": task.id, "output": task.output, "checklist": task.checklist, "needs_write": task.needs_write}
    out = runner.run(ACCEPT, content, context=ctx)
    passed = out.passed and all(i.ok for i in out.items)
    acc = Acceptance(task_id=task.id, checklist=[i.model_dump() for i in out.items], passed=passed, comment=out.comment)
    s.add(acc)
    if passed:
        transition(s, "task", task.id, "accept", CEO)
    elif task.rework_count >= MAX_REWORK:
        transition(s, "task", task.id, "escalate", CEO, {"comment": out.comment})
    else:
        task.rework_count += 1
        transition(s, "task", task.id, "rework", CEO, {"comment": out.comment})
    s.flush()
    return acc


# ---------- 改一改 ----------

class ReviseOut(BaseModel):
    action: str
    reason: str
    evidence: list[str]
    needs_write: bool


REVISE = AgentSpec(
    name="ceo.revise", model_role="ceo", output=ReviseOut,
    numeric_text=lambda o: " ".join([o.action, o.reason, *o.evidence]),
)
MAX_REVISE_ROUNDS = 3


def revise(runner: AgentRunner, s: Session, suggestion_id: int, feedback: str) -> Suggestion:
    """按店主的修改意见改写建议，改完重新进入 reported 等待拍板。数字仍需能在当天数据里找到。"""
    sug = s.get(Suggestion, suggestion_id)
    assert sug is not None and sug.status == "revising"
    content, facts, _ = ops_input(s, sug.day)
    prompt = (
        f"店主对下面这条建议提出了修改意见，请按意见改写。\n原建议：{sug.action}，{sug.reason}\n"
        f"原证据：{sug.evidence}\n店主意见：{feedback}\n\n当天数据：\n{content}"
    )
    ctx = {"suggestion": {"action": sug.action, "reason": sug.reason, "evidence": sug.evidence,
                          "needs_write": sug.needs_write}, "feedback": feedback}
    out = runner.run(REVISE, prompt, facts=facts, context=ctx)
    sug.action, sug.reason, sug.evidence, sug.needs_write = out.action, out.reason, out.evidence, out.needs_write
    sug.revise_rounds += 1
    transition(s, "suggestion", sug.id, "report", CEO, {"feedback": feedback})
    return sug


# ---------- 对话 ----------

class ChatOut(BaseModel):
    reply: str
    instruction: str  # 店主下达的操作指令（没有则为空），会转成建议卡片走审批


def _chat_drop(o: ChatOut, _facts) -> ChatOut:
    return ChatOut(reply="这个问题涉及的数字我在今天的数据里核对不上，先不回答，避免给出错误数字。", instruction=o.instruction)


CHAT = AgentSpec(
    name="ceo.chat", model_role="main", output=ChatOut,
    numeric_text=lambda o: o.reply, drop_invalid=_chat_drop,
)


def chat(runner: AgentRunner, s: Session, text: str) -> ChatOut:
    """回答店主的问题。只基于最近一天程序算好的数据，不调用工具。"""
    day = queries.business_day(s)
    content, facts, ctx = ops_input(s, day)
    return runner.run(CHAT, f"店主问：{text}\n\n可用数据：\n{content}", facts=facts, context={**ctx, "text": text})
