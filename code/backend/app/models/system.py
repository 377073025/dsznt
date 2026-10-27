"""系统表（07 号文档第 11 节）：用户、能力开关、申请进度、作业、事件、审计、成本。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, Date, DateTime, Float, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, utcnow


class User(Base, TimestampMixin):
    __tablename__ = "user"
    id: Mapped[int] = mapped_column(primary_key=True)
    feishu_open_id: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64), default="")
    role: Mapped[str] = mapped_column(String(16), default="viewer")  # owner / operator / viewer


class Shop(Base, TimestampMixin):
    __tablename__ = "shop"
    id: Mapped[int] = mapped_column(primary_key=True)
    platform_shop_id: Mapped[str] = mapped_column(String(64), default="")
    name: Mapped[str] = mapped_column(String(128))
    platform: Mapped[str] = mapped_column(String(16), default="pdd")


class Capability(Base, TimestampMixin):
    __tablename__ = "capability"
    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="not_configured")
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str] = mapped_column(Text, default="")
    credential_ref: Mapped[str] = mapped_column(String(64), default="")
    application_ids: Mapped[list[str]] = mapped_column(JSON, default=list)


class CapabilityEvent(Base):
    __tablename__ = "capability_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(32))
    from_status: Mapped[str] = mapped_column(String(16))
    to_status: Mapped[str] = mapped_column(String(16))
    reason: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Application(Base, TimestampMixin):
    """08 号人工申请清单的进度。"""

    __tablename__ = "application"
    id: Mapped[str] = mapped_column(String(8), primary_key=True)  # R01–R15
    name: Mapped[str] = mapped_column(String(64))
    level: Mapped[str] = mapped_column(String(8))  # 必需 / 增强 / 备用
    status: Mapped[str] = mapped_column(String(16), default="todo")  # todo/doing/waiting/done/rejected/skip
    owner: Mapped[str] = mapped_column(String(64), default="")
    note: Mapped[str] = mapped_column(Text, default="")


class Job(Base, TimestampMixin):
    """通用作业队列：web 与 worker 通过它通信。"""

    __tablename__ = "job"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dedupe_key: Mapped[str | None] = mapped_column(String(128), nullable=True, unique=True)
    status: Mapped[str] = mapped_column(String(16), default="queued")  # queued/running/done/failed
    run_after: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_by: Mapped[str] = mapped_column(String(64), default="")
    locked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error: Mapped[str] = mapped_column(Text, default="")
    lane: Mapped[str] = mapped_column(String(8), default="main")  # main / adhoc（07 号文档 17.4 节）


class DomainEvent(Base):
    __tablename__ = "domain_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    type: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    dispatched: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class FetchJob(Base, TimestampMixin):
    __tablename__ = "fetch_job"
    __table_args__ = (UniqueConstraint("item", "day", name="uq_fetch_item_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    item: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(24), default="scheduled")
    channel: Mapped[str] = mapped_column(String(16), default="")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str] = mapped_column(Text, default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class WebScript(Base, TimestampMixin):
    __tablename__ = "web_script"
    __table_args__ = (UniqueConstraint("task_name", "script_version", name="uq_script_version"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    task_name: Mapped[str] = mapped_column(String(64))
    script_version: Mapped[int] = mapped_column(Integer)
    steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(8), default="dev")  # dev / ai
    state: Mapped[str] = mapped_column(String(16), default="active")  # candidate / active / retired
    success_streak: Mapped[int] = mapped_column(Integer, default=0)


class RiskEvent(Base):
    __tablename__ = "risk_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(32))
    page: Mapped[str] = mapped_column(String(256), default="")
    type: Mapped[str] = mapped_column(String(16))  # captcha / blocked / login_expired
    recent_rate: Mapped[float] = mapped_column(Float, default=0.0)
    handled_by: Mapped[str] = mapped_column(String(64), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class ManualImport(Base, TimestampMixin):
    __tablename__ = "manual_import"
    id: Mapped[int] = mapped_column(primary_key=True)
    file_path: Mapped[str] = mapped_column(String(512))
    parser: Mapped[str] = mapped_column(String(64), default="")
    item: Mapped[str] = mapped_column(String(64), default="")
    rows: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="received")  # received/imported/unrecognized/invalid
    error_rows: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    uploaded_by: Mapped[str] = mapped_column(String(64), default="")


class CardMessage(Base, TimestampMixin):
    """一张卡片可能对应多个实体（早报里每条建议都有按钮），所以按 (message_id, entity, entity_id) 登记。"""

    __tablename__ = "card_message"
    id: Mapped[int] = mapped_column(primary_key=True)
    message_id: Mapped[str] = mapped_column(String(64), index=True)
    card_token: Mapped[str] = mapped_column(String(128), default="")
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[int] = mapped_column(Integer)
    card: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="sent")


class ProcessedAction(Base):
    """卡片回调幂等：同一 entity+id+event+version 只处理一次。"""

    __tablename__ = "processed_action"
    key: Mapped[str] = mapped_column(String(160), primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(64))
    model: Mapped[str] = mapped_column(String(64))
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, default=0)
    batch: Mapped[bool] = mapped_column(Boolean, default=False)
    cost_cny: Mapped[float] = mapped_column(Float, default=0.0)
    lane: Mapped[str] = mapped_column(String(8), default="main")  # 单独任务的预算单独计
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AdhocTask(Base, TimestampMixin):
    """人工提交的单独任务（07 号文档 17.5 节）。结果只存在这里，转为建议 / 候选后才进入主流程。"""

    __tablename__ = "adhoc_task"
    id: Mapped[int] = mapped_column(primary_key=True)
    agent: Mapped[str] = mapped_column(String(32))
    capability: Mapped[str] = mapped_column(String(64))
    params: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    note: Mapped[str] = mapped_column(Text, default="")
    submitted_by: Mapped[str] = mapped_column(String(64))
    # queued / running / done / failed / cancelled / deferred / promoted
    status: Mapped[str] = mapped_column(String(16), default="queued")
    job_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    result: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str] = mapped_column(Text, default="")
    promoted_to: Mapped[str] = mapped_column(String(64), default="")  # 例如 suggestion:12


class AuditLog(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(primary_key=True)
    actor: Mapped[str] = mapped_column(String(64))  # user:<open_id> / agent:<name> / system
    action: Mapped[str] = mapped_column(String(64))
    target: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RuleConfig(Base):
    __tablename__ = "rule_config"
    id: Mapped[int] = mapped_column(primary_key=True)
    key: Mapped[str] = mapped_column(String(128))
    value: Mapped[Any] = mapped_column(JSON)
    rule_version: Mapped[int] = mapped_column(Integer, default=1)
    approved_by: Mapped[str] = mapped_column(String(64), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class LlmBatch(Base, TimestampMixin):
    """提交到模型服务的 Batch（07 号文档 7.4 节）：结果回来后恢复 resume 作业；超过 deadline 未返回则改走实时调用。"""

    __tablename__ = "llm_batch"
    id: Mapped[int] = mapped_column(primary_key=True)
    provider_batch_id: Mapped[str] = mapped_column(String(128), default="")
    agent: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="submitted")  # submitted / ended / fallback
    results: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # custom_id → LLMResult
    resume: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # {type, payload}：结果到了以后要继续的作业
    deadline_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
