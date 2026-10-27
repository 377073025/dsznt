"""系统作业：Batch 轮询、预算告警、周度系统报告、通知（07 号文档 7.4、14 节）。"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data import router
from app.db import runs_scope, utcnow
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import send_card
from app.llm.gateway import Gateway
from app.models import (AgentRun, Application, FetchJob, Job, LlmBatch, LlmUsage, ProcessedAction, Report, RiskEvent,
                        User, WebScript)
from app.services import Services
from app.workflow.jobs import JobContext, enqueue, job_handler

log = logging.getLogger(__name__)


# ---------- Batch ----------

@job_handler("llm.batch_poll")
def _batch_poll(ctx: JobContext, p: dict[str, Any]) -> None:
    """结果到了（或超时改走实时）→ 重新排入原作业；还没到 → poll_minutes 后再查。"""
    s = ctx.session
    b = s.get(LlmBatch, p["batch_id"])
    if b is None or not ctx.gateway.poll_batch(s, b):
        minutes = get_settings().raw["llm_batch"]["poll_minutes"]
        enqueue(s, "llm.batch_poll", p, run_after=utcnow() + timedelta(minutes=minutes))
        return
    if b.status == "fallback":
        log.warning("Batch #%s（%s）超时未返回，改走实时调用", b.id, b.agent)
    r = b.resume
    enqueue(s, r["type"], {**r["payload"], "batch_id": b.id}, dedupe_key=f"{r['type']}:batch:{b.id}")


# ---------- 预算告警 ----------

def _once(s: Session, key: str) -> bool:
    if s.get(ProcessedAction, key) is not None:
        return False
    s.add(ProcessedAction(key=key))
    return True


def check_budget_alerts(s: Session, services: Services, gateway: Gateway | None = None) -> list[str]:
    """主流程花费达到日 / 月预算的 warn_ratio 时提醒一次，达到 100% 时再提醒一次（说明哪些环节会推迟）。"""
    gw = gateway or Gateway(None)
    b = gw.settings.budget
    now_cn = utcnow() + timedelta(hours=8)
    sent = []
    for scope, spent, limit, period in (
        ("日", gw.spent_today(s), b["daily_cny"], now_cn.strftime("%Y-%m-%d")),
        ("月", gw.spent_month(s), b["monthly_cny"], now_cn.strftime("%Y-%m")),
    ):
        level = "over" if spent >= limit else "warn" if spent >= limit * b["warn_ratio"] else ""
        if not level or not _once(s, f"budget:{scope}:{period}:{level}"):
            continue
        if level == "over":
            body = (f"{period} LLM 花费 ¥{spent:.2f}，已达{scope}预算 ¥{limit:.0f}。"
                    f"复盘解读等非关键环节推迟执行，早报、派活、验收、运营诊断照常。可在规则配置里调整预算。")
        else:
            body = f"{period} LLM 花费 ¥{spent:.2f}，已用{scope}预算 ¥{limit:.0f} 的 {spent / limit:.0%}。"
        send_card(s, services, cards.notice(f"预算提醒 · {scope}预算", body, "red" if level == "over" else "orange"),
                  "budget", 0)
        sent.append(f"{scope}:{level}")
    return sent


@job_handler("system.budget_check")
def _budget_check(ctx: JobContext, _p: dict) -> None:
    check_budget_alerts(ctx.session, ctx.services, ctx.gateway)


# ---------- 周度系统报告 ----------

def _cn_day_start(d: date) -> datetime:
    return datetime(d.year, d.month, d.day) - timedelta(hours=8)


def weekly_system_report(s: Session, end: date) -> Report:
    """end 为北京时间的最后一天（含），统计最近 7 天。"""
    start = end - timedelta(days=6)
    t0, t1 = _cn_day_start(start), _cn_day_start(end + timedelta(days=1))
    jobs = s.scalars(select(FetchJob).where(FetchJob.day >= start, FetchJob.day <= end)).all()
    success = sum(j.status == router.SUCCESS for j in jobs)
    risks = Counter(r.type for r in s.scalars(select(RiskEvent).where(RiskEvent.at >= t0, RiskEvent.at < t1)))

    def cost(lane: str) -> float:
        return float(s.scalar(select(func.coalesce(func.sum(LlmUsage.cost_cny), 0.0))
                              .where(LlmUsage.at >= t0, LlmUsage.at < t1, LlmUsage.lane == lane)))

    top = s.execute(select(LlmUsage.agent, func.sum(LlmUsage.cost_cny)).where(LlmUsage.at >= t0, LlmUsage.at < t1)
                    .group_by(LlmUsage.agent).order_by(func.sum(LlmUsage.cost_cny).desc()).limit(3)).all()
    with runs_scope() as rs:
        runs_failed = rs.scalar(select(func.count(AgentRun.id)).where(
            AgentRun.status == "failed", AgentRun.started_at >= t0, AgentRun.started_at < t1)) or 0
    apps = s.scalars(select(Application).order_by(Application.id)).all()
    content = {
        "start": start.isoformat(), "end": end.isoformat(),
        "fetch": {"total": len(jobs), "success": success, "success_rate": success / len(jobs) if jobs else 1.0,
                  "manual": sum(j.channel == "manual" and j.status == router.SUCCESS for j in jobs),
                  "missing": sum(j.status == router.MISSING for j in jobs)},
        "repairs": s.scalar(select(func.count(WebScript.id)).where(
            WebScript.updated_by == "ai", WebScript.created_at >= t0, WebScript.created_at < t1)) or 0,
        "risks": dict(risks), "risk_total": sum(risks.values()),
        "jobs_failed": s.scalar(select(func.count(Job.id)).where(
            Job.status == "failed", Job.updated_at >= t0, Job.updated_at < t1)) or 0,
        "runs_failed": runs_failed,
        "cost": {"main": round(cost("main"), 2), "adhoc": round(cost("adhoc"), 2),
                 "month": round(Gateway(None).spent_month(s), 2),
                 "monthly_budget": get_settings().budget["monthly_cny"],
                 "top_agents": [(a, round(v, 2)) for a, v in top]},
        "applications": {"total": len(apps), "done": sum(a.status == "done" for a in apps),
                         "pending": [f"{a.id} {a.name}（{a.status}）" for a in apps
                                     if a.status not in ("done", "skip") and a.level == "必需"]},
    }
    iso = end.isocalendar()
    period = f"{iso.year}-W{iso.week:02d}"
    report = s.scalar(select(Report).where(Report.kind == "system", Report.period == period))
    if report is None:
        report = Report(kind="system", period=period)
        s.add(report)
    report.content = content
    s.flush()
    return report


@job_handler("system.weekly_report")
def _weekly_report(ctx: JobContext, p: dict[str, Any]) -> None:
    s = ctx.session
    end = date.fromisoformat(p["end"]) if p.get("end") else (utcnow() + timedelta(hours=8)).date()
    report = weekly_system_report(s, end)
    card = cards.system_weekly(report.period, report.content)
    owners = s.scalars(select(User.feishu_open_id).where(User.role == "owner")).all()
    for oid in owners or [""]:  # 还没有店主时发到群里
        send_card(s, ctx.services, card, "report", report.id, to_user=oid)


# ---------- 通知 ----------

@job_handler("system.notify")
def _notify(ctx: JobContext, p: dict[str, Any]) -> None:
    """其他线程（调度器里的备份等）出问题时，通过作业发飞书通知。"""
    send_card(ctx.session, ctx.services, cards.notice(p["title"], p["body"], p.get("template", "red")), "system", 0)
