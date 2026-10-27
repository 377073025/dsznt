"""智能体当前状态（07 号文档 17.3 节）：查询时实时计算，不单独存。

优先级：运行中 > 等待人工 > 已暂停 > 排队中 > 空闲。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import capabilities as caps
from app.agents.catalog import AGENT_TITLES, CATALOG
from app.db import runs_scope
from app.llm.gateway import Gateway
from app.models import AdhocTask, AgentRun, FetchJob, Job, Suggestion, Task

# 主流程作业 → 负责的智能体
JOB_AGENTS: dict[str, tuple[str, ...]] = {
    "daily.fetch": ("web",),
    "daily.analyze": ("ops", "ceo"),
    "ceo.dispatch": ("ceo",),
    "ceo.accept": ("ceo",),
    "selection.weekly": ("selection",),
    "selection.prepare": ("selection",),
    "task.execute": ("exec",),
    "task.write": ("exec",),
}
# 主流程里各智能体依赖的能力开关（每组任意一个 healthy 即可）
AGENT_NEEDS: dict[str, tuple[tuple[str, ...], ...]] = {
    "ops": (("llm",), ("pdd_api", "pdd_web")),
    "ceo": (("llm",),),
    "selection": (("llm",), ("alibaba_api", "alibaba_web"), ("market_tool",)),
    "exec": (("llm",), ("dropship_tool",)),
    "web": (("pdd_api", "pdd_web"),),
}


def _run_dict(r: AgentRun) -> dict[str, Any]:
    return {
        "id": r.id, "capability": r.capability, "source": r.source, "label": r.label, "status": r.status,
        "current_step": r.current_step, "model": r.model, "turns": r.turns, "tokens": r.tokens, "cost_fen": r.cost_fen,
        "started_at": r.started_at.isoformat(), "finished_at": r.finished_at.isoformat() if r.finished_at else None,
        "error": r.error, "adhoc_task_id": r.adhoc_task_id,
    }


def agents_status(s: Session) -> list[dict[str, Any]]:
    with runs_scope() as rs:
        running = rs.scalars(select(AgentRun).where(AgentRun.status == "running").order_by(AgentRun.id)).all()
        last = {a: rs.scalar(select(AgentRun).where(AgentRun.agent == a, AgentRun.status != "running")
                             .order_by(AgentRun.id.desc()).limit(1)) for a in AGENT_TITLES}
        running_d = [(r.agent, _run_dict(r)) for r in running]
        last_d = {a: _run_dict(r) if r else None for a, r in last.items()}

    queued: dict[str, list[str]] = {a: [] for a in AGENT_TITLES}
    for j in s.scalars(select(Job).where(Job.status == "queued", Job.lane == "main")):
        for a in JOB_AGENTS.get(j.type, ()):
            queued[a].append(f"主流程 · {j.type}")
    for t in s.scalars(select(AdhocTask).where(AdhocTask.status.in_(("queued", "deferred")))):
        queued[t.agent].append(f"单独任务 #{t.id} · {CATALOG[t.capability].title}" + (f"（{t.error}）" if t.error else ""))

    waiting: dict[str, list[str]] = {a: [] for a in AGENT_TITLES}
    for t in s.scalars(select(Task).where(Task.status == "awaiting_write_confirm")):
        waiting["exec"].append(f"任务 #{t.id} 等待写入确认")
    n_reported = len(s.scalars(select(Suggestion.id).where(Suggestion.status == "reported")).all())
    if n_reported:
        waiting["ceo"].append(f"{n_reported} 条建议等待店主拍板")
    for j in s.scalars(select(FetchJob).where(FetchJob.status.in_(("waiting_human", "circuit_open", "manual_requested")))):
        what = {"waiting_human": "验证码 / 登录", "circuit_open": "熔断待店主确认", "manual_requested": "等待人工导出"}[j.status]
        waiting["web"].append(f"{j.day} {j.item}：{what}")

    gw = Gateway(None)
    over_budget = gw.spent_today(s) >= gw.settings.budget["daily_cny"]
    out = []
    for a, title in AGENT_TITLES.items():
        paused = [f"需要 {'/'.join(g)}" for g in AGENT_NEEDS.get(a, ()) if not any(caps.is_healthy(s, k) for k in g)]
        if over_budget:
            paused.append("今日 LLM 预算已用完（关键环节除外）")
        runs = [r for ag, r in running_d if ag == a]
        if runs:
            state = "running"
        elif waiting[a]:
            state = "waiting_human"
        elif paused:
            state = "paused"
        elif queued[a]:
            state = "queued"
        else:
            state = "idle"
        out.append({
            "agent": a, "title": title, "state": state, "running": runs, "waiting": waiting[a], "paused": paused,
            "queued": queued[a], "last_run": last_d[a],
            "capabilities": [c.key for c in CATALOG.values() if c.agent == a],
        })
    return out


def runs_of(agent: str, source: str | None = None, limit: int = 50, offset: int = 0) -> list[dict[str, Any]]:
    with runs_scope() as rs:
        q = select(AgentRun).where(AgentRun.agent == agent)
        if source:
            q = q.where(AgentRun.source == source)
        return [_run_dict(r) for r in rs.scalars(q.order_by(AgentRun.id.desc()).offset(offset).limit(limit))]


def run_detail(run_id: int) -> dict[str, Any] | None:
    from app.models import AgentRunStep

    with runs_scope() as rs:
        r = rs.get(AgentRun, run_id)
        if r is None:
            return None
        steps = rs.scalars(select(AgentRunStep).where(AgentRunStep.run_id == run_id).order_by(AgentRunStep.seq))
        return {**_run_dict(r), "steps": [{"seq": x.seq, "kind": x.kind, "name": x.name, "summary": x.summary,
                                             "tokens": x.tokens, "at": x.at.isoformat()} for x in steps]}
