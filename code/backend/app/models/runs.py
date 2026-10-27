"""运行记录（07 号文档 17.3 节），存放在独立的运行记录库（见 db.RunBase）。"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import RunBase, utcnow


class AgentRun(RunBase):
    __tablename__ = "agent_run"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(32), index=True)       # 智能体：ops / ceo / selection / exec / web
    capability: Mapped[str] = mapped_column(String(64))              # 环节或能力，例如 ops.diagnose
    source: Mapped[str] = mapped_column(String(8), default="main")   # main / adhoc
    label: Mapped[str] = mapped_column(String(128), default="")      # 「主流程 · daily.analyze」「单独任务 #12」
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    adhoc_task_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), default="running")  # running / succeeded / failed / cancelled
    current_step: Mapped[str] = mapped_column(String(128), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    turns: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_fen: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")


class AgentRunStep(RunBase):
    __tablename__ = "agent_run_step"
    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    seq: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(8))     # llm / tool / check
    name: Mapped[str] = mapped_column(String(64))
    summary: Mapped[str] = mapped_column(Text, default="")  # 截断到 500 字
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
