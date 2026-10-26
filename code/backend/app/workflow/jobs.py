"""通用作业队列：web 进程只负责 enqueue，worker 领取执行。所有耗时工作都在这里跑。"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.agents.runtime import AgentRunner
from app.db import session_scope, utcnow
from app.llm.gateway import BudgetExceeded, Gateway
from app.models import Job
from app.services import Services
from app.workflow.events import dispatch_pending

log = logging.getLogger(__name__)
MAX_ATTEMPTS = 3


@dataclass
class JobContext:
    session: Session
    services: Services
    gateway: Gateway

    @property
    def runner(self) -> AgentRunner:
        return AgentRunner(self.session, self.services, self.gateway)


JobHandler = Callable[[JobContext, dict[str, Any]], None]
JOB_HANDLERS: dict[str, JobHandler] = {}


def job_handler(type_: str) -> Callable[[JobHandler], JobHandler]:
    def deco(fn: JobHandler) -> JobHandler:
        JOB_HANDLERS[type_] = fn
        return fn

    return deco


def enqueue(s: Session, type_: str, payload: dict[str, Any], dedupe_key: str | None = None,
            run_after: datetime | None = None) -> Job | None:
    """dedupe_key 相同的作业只入队一次（已完成的也不再重复）。"""
    if dedupe_key and s.scalar(select(Job.id).where(Job.dedupe_key == dedupe_key)) is not None:
        return None
    job = Job(type=type_, payload=payload, dedupe_key=dedupe_key, run_after=run_after or utcnow())
    s.add(job)
    s.flush()
    return job


def _claim(s: Session, worker_id: str) -> Job | None:
    job = s.scalar(
        select(Job).where(Job.status == "queued", Job.run_after <= utcnow()).order_by(Job.id).limit(1)
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


def run_one(services: Services, gateway: Gateway, worker_id: str = "worker") -> bool:
    with session_scope() as s:
        job = _claim(s, worker_id)
        if job is None:
            return False
        job_id, job_type, payload, attempts = job.id, job.type, dict(job.payload), job.attempts
    try:
        with session_scope() as s:
            JOB_HANDLERS[job_type](JobContext(s, services, gateway), payload)
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


def run_until_idle(services: Services, gateway: Gateway, max_rounds: int = 500) -> int:
    """交替分发事件和执行作业，直到都处理完。用于测试、演示和 worker 的每轮循环。"""
    done = 0
    for _ in range(max_rounds):
        n_events = dispatch_events()
        ran = run_one(services, gateway)
        done += int(ran)
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
