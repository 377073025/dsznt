"""通用作业队列：web 进程只负责 enqueue，worker 领取执行。所有耗时工作都在这里跑。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRunner
from app.agents.tracking import RunTracker
from app.db import session_scope, utcnow
from app.llm.gateway import BudgetExceeded, Gateway
from app.logs import log_context
from app.models import Job
from app.services import Services
from app.workflow import rule_config
from app.workflow.events import dispatch_pending

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


LANES = ("main", "adhoc")


@dataclass
class JobContext:
    session: Session
    services: Services
    gateway: Gateway
    job_id: int | None = None
    job_type: str = ""
    lane: str = "main"

    @property
    def runner(self) -> AgentRunner:
        tracker = RunTracker(source=self.lane, label=f"主流程 · {self.job_type}" if self.lane == "main" else self.job_type,
                             job_id=self.job_id)
        return AgentRunner(self.session, self.services, self.gateway, tracker=tracker, lane=self.lane)


JobHandler = Callable[[JobContext, dict[str, Any]], None]
JOB_HANDLERS: dict[str, JobHandler] = {}


def job_handler(type_: str) -> Callable[[JobHandler], JobHandler]:
    def deco(fn: JobHandler) -> JobHandler:
        JOB_HANDLERS[type_] = fn
        return fn

    return deco


def enqueue(s: Session, type_: str, payload: dict[str, Any], dedupe_key: str | None = None,
            run_after: datetime | None = None, lane: str = "main") -> Job | None:
    """dedupe_key 相同的作业只入队一次（已完成的也不再重复）。"""
    if dedupe_key and s.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key)) is not None:
        return None
    job = Job(type=type_, payload=payload, dedupe_key=dedupe_key, run_after=run_after or utcnow(), lane=lane)
    s.add(job)
    s.flush()
    return job


def _claim(s: Session, worker_id: str, lane: str = "main") -> Job | None:
    job = s.scalar(
        select(Job).where(Job.status == "queued", Job.lane == lane, Job.run_after <= utcnow()).order_by(Job.id).limit(1)
    )
    if job is None:
        return None
    res = s.execute(
        update(Job).where(Job.id == job.id, Job.status == "queued")
        .values(status="running", locked_by=worker_id, locked_at=utcnow(), attempts=Job.attempts + 1)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        return None
    s.refresh(job)
    return job


def run_one(services: Services, gateway: Gateway, worker_id: str = "worker", lane: str = "main") -> bool:
    with session_scope() as s:
        job = _claim(s, worker_id, lane)
        if job is None:
            return False
        job_id, job_type, payload, attempts = job.id, job.type, dict(job.payload), job.attempts
    try:
        with log_context(job_id=job_id, job_type=job_type, lane=lane), session_scope() as s:
            rule_config.apply_latest(s)  # 店主在 Web 改过规则时，worker 从下一个作业起生效
            JOB_HANDLERS[job_type](JobContext(s, services, gateway, job_id, job_type, lane), payload)
            s.get(Job, job_id).status = "done"
    except BudgetExceeded as e:
        _reschedule(job_id, str(e), timedelta(hours=12), count_attempt=False)
    except Exception as e:  # noqa: BLE001
        log.exception("job %s #%s failed", job_type, job_id)
        if attempts >= MAX_ATTEMPTS:
            with session_scope() as s:
                j = s.get(Job, job_id)
                j.status, j.error = "failed", f"{type(e).__name__}: {e}"[:2000]
        else:
            _reschedule(job_id, f"{type(e).__name__}: {e}", timedelta(minutes=2 ** attempts))
    return True


def _reschedule(job_id: int, error: str, delay: timedelta, count_attempt: bool = True) -> None:
    with session_scope() as s:
        j = s.get(Job, job_id)
        j.status, j.error, j.run_after = "queued", error[:2000], utcnow() + delay
        if not count_attempt:
            j.attempts -= 1


def dispatch_events() -> int:
    with session_scope() as s:
        return dispatch_pending(s)


def run_until_idle(services: Services, gateway: Gateway, max_rounds: int = 500,
                   lanes: tuple[str, ...] = LANES, dispatch: bool = True) -> int:
    """交替分发事件和执行作业，直到都处理完。用于测试、演示和 worker 的每轮循环。

    worker 里两个通道各跑一个循环（lanes 只给一个）；事件只由主通道循环分发（dispatch）。
    """
    done = 0
    for _ in range(max_rounds):
        n_events = dispatch_events() if dispatch else 0
        ran = 0
        for lane in lanes:
            ran += int(run_one(services, gateway, worker_id=f"worker-{lane}", lane=lane))
        done += ran
        if not n_events and not ran:
            break
    return done


def recover_stale(timeout: timedelta = timedelta(minutes=30)) -> int:
    """主机重启后：running 超时的作业重置为 queued。"""
    with session_scope() as s:
        res = s.execute(
            update(Job).where(Job.status == "running", Job.locked_at < utcnow() - timeout)
            .values(status="queued", locked_by="")
        )
        return res.rowcount or 0
