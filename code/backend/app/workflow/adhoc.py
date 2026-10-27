"""单独任务（07 号文档 17.5 节）：人工提交 → 单独通道执行同一份能力 → 结果只进 adhoc_task。

结果隔离的三条硬规则：
1. 单独通道的工具上下文 lane=adhoc，非只读工具一律拒绝并写审计（tools/registry.py）。
2. 只产生 adhoc.* 事件，主流程的订阅者不处理。
3. 能力运行期间挂一个 before_flush 守卫：除了模型计量、审计和 adhoc.* 事件，写任何表都会被拦下。
转为建议 / 候选之后，完全走主流程的状态机和人工闸门。
"""
from __future__ import annotations

import logging
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

from sqlalchemy import event, func, select
from sqlalchemy.orm import Session

from app.adapters.types import OfferDetail
from app.agents import ceo, selection
from app.agents.catalog import CATALOG, IMAGE_MODES, CapContext, missing_capabilities
from app.agents.runtime import AgentRunner
from app.agents.tracking import RunCancelled, RunTracker, request_cancel
from app.data import queries
from app.data.channels.web_reader.browser import BrowserBusy, blackout_end, browser_lock, to_utc
from app.db import runs_scope, utcnow
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import send_card
from app.llm.gateway import BudgetExceeded
from app.models import AdhocTask, AgentRun, AuditLog, Candidate, Demand, DomainEvent, Job, LlmUsage, Shop, Suggestion, User
from app.services import Services
from app.workflow.events import audit, emit
from app.workflow.jobs import JobContext, enqueue, job_handler
from app.workflow.state_machine import SYSTEM, transition

log = logging.getLogger(__name__)
SUBMIT_ROLES = ("owner", "operator")
BROWSER_RETRY = timedelta(minutes=10)


class AdhocError(Exception):
    """提交 / 取消 / 转化时的业务错误，web 层转成 4xx。"""


class IsolationViolation(Exception):
    pass


# ---------- 隔离守卫 ----------

def _allowed(obj: Any) -> bool:
    if isinstance(obj, (LlmUsage, AuditLog)):
        return True
    return isinstance(obj, DomainEvent) and obj.type.startswith("adhoc.")


def _guard(session: Session, _ctx: Any, _instances: Any) -> None:
    for obj in [*session.new, *session.dirty, *session.deleted]:
        if not _allowed(obj) and (obj in session.new or obj in session.deleted or session.is_modified(obj)):
            raise IsolationViolation(f"单独任务不能写 {type(obj).__tablename__}")


@contextmanager
def isolated(s: Session) -> Iterator[None]:
    s.flush()
    event.listen(s, "before_flush", _guard)
    try:
        yield
    except Exception:
        # 被拦下的写入从来没有落库；把它们从会话里清掉，免得提交时再写一次
        for obj in list(s.new):
            if not _allowed(obj):
                s.expunge(obj)
        for obj in list(s.dirty):
            if not _allowed(obj):
                s.expire(obj)
        raise
    finally:
        event.remove(s, "before_flush", _guard)


# ---------- 提交 ----------

def submit(s: Session, user: User, capability: str, params: dict[str, Any], note: str = "") -> AdhocTask:
    if user.role not in SUBMIT_ROLES:
        raise PermissionError("只有店主和运营可以提交单独任务")
    cap = CATALOG.get(capability)
    if cap is None or not cap.adhoc:
        raise AdhocError(f"能力 {capability} 不存在或不允许单独调用")
    parsed = cap.params.model_validate(params)  # 参数不对直接抛 ValidationError
    task = AdhocTask(agent=cap.agent, capability=cap.key, params=parsed.model_dump(mode="json"), note=note,
                     submitted_by=user.feishu_open_id, status="queued", result={})
    s.add(task)
    s.flush()
    task.job_id = enqueue(s, "adhoc.run", {"adhoc_task_id": task.id}, lane="adhoc").id
    audit(s, f"user:{user.feishu_open_id}", "adhoc.submit", f"adhoc_task:{task.id}", {"capability": cap.key})
    return task


def _now_cn() -> datetime:
    return utcnow() + timedelta(hours=8)


def _defer(s: Session, task: AdhocTask, run_after: datetime, reason: str) -> None:
    task.status, task.error = "deferred", reason
    task.job_id = enqueue(s, "adhoc.run", {"adhoc_task_id": task.id}, run_after=run_after, lane="adhoc").id


def _next_day_utc() -> datetime:
    tomorrow = _now_cn().date() + timedelta(days=1)
    return to_utc(datetime.combine(tomorrow, datetime.min.time()) + timedelta(minutes=5))


def _latest_run_id(task_id: int) -> int | None:
    with runs_scope() as rs:
        return rs.scalar(select(func.max(AgentRun.id)).where(AgentRun.adhoc_task_id == task_id))


@job_handler("adhoc.run")
def _run(ctx: JobContext, p: dict[str, Any]) -> None:
    s = ctx.session
    task = s.get(AdhocTask, p["adhoc_task_id"])
    if task is None or task.status not in ("queued", "deferred"):
        return  # 已取消或已处理
    cap = CATALOG[task.capability]
    missing = missing_capabilities(s, cap)
    if missing:
        task.status, task.error = "failed", f"已暂停：需要的能力未就绪（{'、'.join(missing)}），恢复后可重新提交"
        emit(s, "adhoc.failed", "adhoc_task", task.id, {"error": task.error})
        return
    if cap.uses_web:
        end = blackout_end(_now_cn())
        if end is not None:
            _defer(s, task, to_utc(end), f"主流程采集时段，网页读取顺延到 {end:%H:%M}")
            return

    task.status, task.error = "running", ""
    tracker = RunTracker(source="adhoc", label=f"单独任务 #{task.id} · {cap.title}", job_id=ctx.job_id,
                         adhoc_task_id=task.id)
    runner = AgentRunner(s, ctx.services, ctx.gateway, tracker=tracker, lane="adhoc")
    try:
        with ExitStack() as stack:
            for platform in sorted(cap.uses_web):
                stack.enter_context(browser_lock(platform, lane="adhoc"))
            with isolated(s):
                out = cap.run(CapContext(s, ctx.services, runner), cap.params.model_validate(task.params))
    except BrowserBusy as e:
        _defer(s, task, utcnow() + BROWSER_RETRY, str(e))
        return
    except BudgetExceeded as e:
        _defer(s, task, _next_day_utc(), str(e))
        return
    except RunCancelled:
        task.status = "cancelled"
    except Exception as e:  # noqa: BLE001  单独任务失败不自动重试，由人决定是否重新提交
        log.warning("adhoc task #%s failed: %s", task.id, e)
        task.status, task.error = "failed", f"{type(e).__name__}: {e}"[:2000]
    else:
        task.status, task.result = "done", out.model_dump(mode="json")
    task.run_id = _latest_run_id(task.id)
    emit(s, f"adhoc.{task.status}", "adhoc_task", task.id, {"capability": cap.key, "submitted_by": task.submitted_by})


# ---------- 取消 ----------

def cancel(s: Session, user: User, task_id: int) -> AdhocTask:
    task = s.get(AdhocTask, task_id)
    if task is None:
        raise AdhocError("单独任务不存在")
    if user.role != "owner" and not (user.role == "operator" and user.feishu_open_id == task.submitted_by):
        raise PermissionError("只有提交人或店主可以取消")
    if task.status in ("queued", "deferred"):
        task.status = "cancelled"
        job = s.get(Job, task.job_id) if task.job_id else None
        if job is not None and job.status == "queued":
            job.status, job.error = "done", "单独任务已取消"
    elif task.status == "running":
        request_cancel(task.id)  # 运行器在下一轮开始前停止
    else:
        raise AdhocError(f"状态 {task.status} 不能取消")
    audit(s, f"user:{user.feishu_open_id}", "adhoc.cancel", f"adhoc_task:{task.id}", {"status": task.status})
    return task


# ---------- 转为建议 / 候选 ----------

def can_promote(task: AdhocTask) -> bool:
    """结果里有可转化的内容：评估通过、货源过了硬性过滤、诊断有建议。通知卡片据此决定是否带按钮。"""
    cap = CATALOG[task.capability]
    if task.status != "done" or cap.promote_to is None:
        return False
    r = task.result
    if task.capability == "selection.evaluate_offer":
        return bool(r.get("passed"))
    if task.capability == "exec.listing":
        return not r.get("reject_reasons")
    if task.capability == "ops.diagnose":
        return bool(r.get("suggestions"))
    return True


def promote(s: Session, services: Services, user: User, task_id: int, indexes: list[int] | None = None) -> str:
    """把单独任务的结果交给主流程：建议进入「待拍板」，候选进入「已推荐」，之后照常走人工闸门。"""
    if user.role not in SUBMIT_ROLES:
        raise PermissionError("只有店主和运营可以转化单独任务结果")
    task = s.get(AdhocTask, task_id)
    if task is None:
        raise AdhocError("单独任务不存在")
    if task.status != "done":
        raise AdhocError(f"状态 {task.status} 不能转化（只有已完成的任务可以）")
    cap = CATALOG[task.capability]
    if cap.promote_to == "suggestion":
        sugs = _to_suggestions(s, task, indexes)
        send_card(s, services, cards.suggestions(f"来自单独任务 #{task.id} 的建议", sugs), "suggestion", sugs[0].id,
                  also=[("suggestion", x.id) for x in sugs[1:]])
        ref = "suggestion:" + ",".join(str(x.id) for x in sugs)
    elif cap.promote_to == "candidate":
        cand = _to_candidate(s, task)
        send_card(s, services, cards.selection_weekly(f"单独任务 #{task.id}", [cand], {cand.offer_id: task.result["title"]}),
                  "candidate", cand.id)
        ref = f"candidate:{cand.id}"
    else:
        raise AdhocError(f"「{cap.title}」的结果不能转化")
    task.status, task.promoted_to = "promoted", ref
    audit(s, f"user:{user.feishu_open_id}", "adhoc.promote", f"adhoc_task:{task.id}", {"to": ref})
    return ref


def _to_suggestions(s: Session, task: AdhocTask, indexes: list[int] | None) -> list[Suggestion]:
    r, prm = task.result, task.params
    origin = f"（来自单独任务 #{task.id}）"
    if task.capability == "ops.diagnose":
        items = r["suggestions"]
        picked = [items[i] for i in indexes] if indexes else items
        drafts = [dict(goods_id=x["goods_id"], type=x["type"], action=x["action"], reason=x["reason"] + origin,
                       evidence=x["evidence"], confidence=x["confidence"], needs_write=x["needs_write"]) for x in picked]
    elif task.capability == "exec.copy":
        drafts = [dict(goods_id=prm.get("goods_id", ""), type="title", action=f"建议把标题改为「{r['title']}」",
                       reason=f"按要求「{prm['instruction']}」生成{origin}", evidence=r["selling_points"],
                       confidence="medium", needs_write=True)]
    elif task.capability == "exec.image":
        drafts = [dict(goods_id=r["goods_id"], type="main_image",
                       action=f"建议换用新主图（{IMAGE_MODES.get(r.get('mode', ''), '修图')}草稿 {r['path']}）",
                       reason=f"按要求「{r['instruction']}」生成{origin}", evidence=[], confidence="medium", needs_write=True)]
    else:
        raise AdhocError(f"{task.capability} 没有转为建议的规则")
    if not drafts:
        raise AdhocError("结果里没有可转化的建议")
    shop = s.scalar(select(Shop).order_by(Shop.id))
    out = []
    for d in drafts:
        sug = Suggestion(shop_id=shop.id, day=queries.business_day(s), **d)
        s.add(sug)
        s.flush()
        transition(s, "suggestion", sug.id, "report", ceo.CEO, {"adhoc_task_id": task.id})
        out.append(sug)
    return out


def _to_candidate(s: Session, task: AdhocTask) -> Candidate:
    """单品评估：通过才能转；物料包：通过硬性过滤才能转，理由注明来自物料包（分数未计需求热度）。"""
    r = task.result
    if task.capability == "exec.listing":
        if r["reject_reasons"]:
            raise AdhocError("货源未通过硬性过滤，不能转为候选：" + "；".join(r["reject_reasons"]))
        reason = (f"来自单独任务 #{task.id} 的上架物料包（标题「{r['package']['title']}」），"
                  "未经需求侦察，分数未计需求热度")
        risks, heat, trend = ["需求热度未评估"], 0.0, 0.0
    else:
        if not r.get("passed"):
            raise AdhocError("单品评估未通过，不能转为候选")
        reason, risks, heat, trend = r["reason"], r["risks"], r["heat"], r["trend"]
    offer = selection._upsert_offer(s, OfferDetail.model_validate(r["offer"]))
    iso = _now_cn().date().isocalendar()
    demand = Demand(week=f"{iso.year}-W{iso.week:02d}", keyword=r["keyword"], seed_type="adhoc", seed_ref=f"adhoc:{task.id}",
                    heat=heat, trend=trend, competitors=[])
    s.add(demand)
    s.flush()
    cand = Candidate(demand_id=demand.id, offer_id=offer.id, price_plan=r["price_plan"], score=r["score"],
                     breakdown=r["breakdown"], reason=reason, risks=risks, status="scored")
    s.add(cand)
    s.flush()
    transition(s, "candidate", cand.id, "recommend", SYSTEM, {"adhoc_task_id": task.id})
    return cand
