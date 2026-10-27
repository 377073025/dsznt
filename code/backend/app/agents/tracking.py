"""运行记录（07 号文档 17.3 节）：AgentRunner 在每个时点写运行记录库，供工作台查看「此刻在干什么」。

每次写入都是运行记录库里的独立短事务，不受业务事务提交或回滚影响。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import delete, select, update

from app.config import get_settings

from app.db import runs_scope, utcnow
from app.models import AgentRun, AgentRunStep

SUMMARY_MAX = 500


class RunCancelled(Exception):
    """人工取消：运行器在下一轮开始前停止。"""


@dataclass
class RunTracker:
    """运行来源。一个作业可能跑多个智能体环节，每个环节一条 agent_run。"""

    source: str = "main"            # main / adhoc
    label: str = ""
    job_id: int | None = None
    adhoc_task_id: int | None = None

    def open(self, capability: str, model: str = "") -> "RunHandle":
        with runs_scope() as rs:
            run = AgentRun(agent=capability.split(".", 1)[0], capability=capability, source=self.source,
                           label=self.label, job_id=self.job_id, adhoc_task_id=self.adhoc_task_id,
                           model=model, current_step="开始")
            rs.add(run)
            rs.flush()
            return RunHandle(run.id)


class RunHandle:
    def __init__(self, run_id: int):
        self.run_id = run_id
        self._seq = 0

    def _set(self, **values) -> None:
        with runs_scope() as rs:
            rs.execute(update(AgentRun).where(AgentRun.id == self.run_id).values(**values))

    def progress(self, current_step: str) -> None:
        self._set(current_step=current_step[:128])

    def step(self, kind: str, name: str, summary: str = "", tokens: int = 0, current: str | None = None) -> None:
        self._seq += 1
        with runs_scope() as rs:
            rs.add(AgentRunStep(run_id=self.run_id, seq=self._seq, kind=kind, name=name[:64],
                                summary=summary[:SUMMARY_MAX], tokens=tokens))
            if current is not None:
                rs.execute(update(AgentRun).where(AgentRun.id == self.run_id).values(current_step=current[:128]))

    def turn_done(self, tokens: int, cost_fen: int) -> None:
        """每轮结束：累加用量，并检查是否被取消。"""
        with runs_scope() as rs:
            run = rs.get(AgentRun, self.run_id)
            run.turns += 1
            run.tokens += tokens
            run.cost_fen += cost_fen
            cancelled = run.cancel_requested
        if cancelled:
            raise RunCancelled(f"运行 #{self.run_id} 已被取消")

    def finish(self, status: str, error: str = "") -> None:
        self._set(status=status, finished_at=utcnow(), error=error[:2000], current_step="")


def request_cancel(adhoc_task_id: int) -> bool:
    with runs_scope() as rs:
        res = rs.execute(update(AgentRun).where(AgentRun.adhoc_task_id == adhoc_task_id, AgentRun.status == "running")
                         .values(cancel_requested=True))
        return bool(res.rowcount)


def recover_stale_runs(timeout: timedelta = timedelta(minutes=30)) -> int:
    """worker 重启时：运行中超过 30 分钟的记录标记为失败。"""
    with runs_scope() as rs:
        res = rs.execute(update(AgentRun).where(AgentRun.status == "running", AgentRun.started_at < utcnow() - timeout)
                         .values(status="failed", finished_at=utcnow(), error="worker 重启，运行中断"))
        return res.rowcount or 0


def purge_old_runs() -> tuple[int, int]:
    """按 adhoc.run_retention_days / step_retention_days 清理运行记录；运行中的不动。返回（删除的运行数, 步骤数）。"""
    conf = get_settings().adhoc
    now = utcnow()
    with runs_scope() as rs:
        old_runs = select(AgentRun.id).where(AgentRun.status != "running",
                                             AgentRun.started_at < now - timedelta(days=conf["run_retention_days"]))
        n_steps = rs.execute(delete(AgentRunStep).where(
            AgentRunStep.run_id.in_(old_runs) | (AgentRunStep.at < now - timedelta(days=conf["step_retention_days"])))
        ).rowcount or 0
        n_runs = rs.execute(delete(AgentRun).where(AgentRun.id.in_(old_runs))).rowcount or 0
        return n_runs, n_steps
