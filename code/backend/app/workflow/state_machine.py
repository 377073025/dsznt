"""状态机（02 号文档第 5 节；实现见 07 号文档 8.1 节）。

所有实体的状态变化都走 transition()：查转换表 → 校验执行者 → 乐观锁更新 → 审计 → 领域事件。
人工闸门（拍板、写入确认、熔断恢复、测款结论）只允许人类角色触发，智能体一律拒绝。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session

from app.models import Candidate, RuleProposal, Suggestion, Task
from app.workflow.events import audit, emit

HUMAN_ROLES = {"owner", "operator"}


@dataclass(frozen=True)
class Actor:
    kind: str  # user / agent / system
    id: str
    role: str = ""  # 仅 user：owner / operator / viewer

    @property
    def label(self) -> str:
        return f"{self.kind}:{self.id}"


SYSTEM = Actor("system", "system")


@dataclass(frozen=True)
class Transition:
    to: str
    allowed: frozenset[str]  # 角色名，或 "agent" / "system"


def _t(to: str, *allowed: str) -> Transition:
    return Transition(to, frozenset(allowed))


OWNER = "owner"
MACHINE = ("agent", "system")

TRANSITIONS: dict[tuple[str, str, str], Transition] = {
    # 运营建议
    ("suggestion", "generated", "report"): _t("reported", *MACHINE),
    ("suggestion", "generated", "discard"): _t("discarded", *MACHINE),
    ("suggestion", "reported", "approve"): _t("approved", OWNER),
    ("suggestion", "reported", "reject"): _t("rejected", OWNER),
    ("suggestion", "reported", "revise"): _t("revising", OWNER),
    ("suggestion", "reported", "expire"): _t("expired", "system"),
    ("suggestion", "revising", "report"): _t("reported", *MACHINE),
    ("suggestion", "revising", "resubmit"): _t("reported", OWNER),  # 改了 3 轮后店主在 Web 直接编辑
    ("suggestion", "revising", "expire"): _t("expired", "system"),
    ("suggestion", "approved", "dispatch"): _t("dispatched", *MACHINE),
    ("suggestion", "dispatched", "complete"): _t("done", *MACHINE),
    ("suggestion", "done", "review"): _t("reviewed", *MACHINE),
    # 选品候选
    ("candidate", "discovered", "filter"): _t("filtered", *MACHINE),
    ("candidate", "discovered", "drop"): _t("rejected", *MACHINE),
    ("candidate", "filtered", "verify"): _t("verified", *MACHINE),
    ("candidate", "filtered", "drop"): _t("rejected", *MACHINE),
    ("candidate", "verified", "score"): _t("scored", *MACHINE),
    ("candidate", "scored", "recommend"): _t("recommended", *MACHINE),
    ("candidate", "scored", "drop"): _t("rejected", *MACHINE),
    ("candidate", "recommended", "approve"): _t("approved", OWNER),
    ("candidate", "recommended", "reject"): _t("rejected", OWNER),
    ("candidate", "recommended", "revise"): _t("revising", OWNER),
    ("candidate", "revising", "recommend"): _t("recommended", *MACHINE),
    ("candidate", "approved", "prepare"): _t("preparing", *MACHINE),
    ("candidate", "preparing", "list"): _t("listed", *MACHINE),
    ("candidate", "listed", "start_test"): _t("testing", *MACHINE),
    ("candidate", "testing", "keep"): _t("kept", OWNER),
    ("candidate", "testing", "optimize"): _t("optimizing", OWNER),
    ("candidate", "testing", "drop"): _t("delisted", OWNER),
    ("candidate", "optimizing", "retest"): _t("testing", *MACHINE),  # 运营优化后再观察 7 天
    # 任务
    ("task", "pending", "start"): _t("running", *MACHINE),
    ("task", "running", "produce"): _t("produced", *MACHINE),
    ("task", "running", "fail"): _t("failed", *MACHINE),
    ("task", "produced", "request_write"): _t("awaiting_write_confirm", *MACHINE),
    ("task", "produced", "submit"): _t("submitted", *MACHINE),
    ("task", "awaiting_write_confirm", "confirm"): _t("writing", OWNER),
    ("task", "awaiting_write_confirm", "cancel"): _t("produced", OWNER, "system"),
    ("task", "writing", "submit"): _t("submitted", *MACHINE),
    ("task", "writing", "fail"): _t("failed", *MACHINE),
    ("task", "submitted", "accept"): _t("accepted", *MACHINE),
    ("task", "submitted", "rework"): _t("pending", *MACHINE),
    ("task", "submitted", "escalate"): _t("escalated", *MACHINE),
    # 规则调整提议（月度调权）：店主同意才生效
    ("rule_proposal", "pending", "apply"): _t("applied", OWNER),
    ("rule_proposal", "pending", "reject"): _t("rejected", OWNER),
    ("rule_proposal", "pending", "expire"): _t("expired", "system"),  # 下个月的新提议替代
}

MODELS = {"suggestion": Suggestion, "candidate": Candidate, "task": Task, "rule_proposal": RuleProposal}


class TransitionDenied(Exception):
    pass


class ConcurrentUpdate(Exception):
    pass


def can(entity: str, status: str, event: str, actor: Actor) -> bool:
    t = TRANSITIONS.get((entity, status, event))
    return t is not None and _allowed(t, actor)


def _allowed(t: Transition, actor: Actor) -> bool:
    if actor.kind == "user":
        return actor.role in t.allowed
    return actor.kind in t.allowed


def transition(s: Session, entity: str, obj_id: int, event: str, actor: Actor, payload: dict[str, Any] | None = None):
    model = MODELS[entity]
    s.flush()  # 先落盘其他字段的修改，下面的 refresh 才不会把它们丢掉
    obj = s.get(model, obj_id)
    if obj is None:
        raise TransitionDenied(f"{entity} #{obj_id} 不存在")
    t = TRANSITIONS.get((entity, obj.status, event))
    if t is None:
        raise TransitionDenied(f"{entity} #{obj_id} 当前状态 {obj.status} 不能执行 {event}")
    if not _allowed(t, actor):
        raise TransitionDenied(f"{actor.label}（{actor.role or actor.kind}）无权对 {entity} 执行 {event}")

    from_status, version = obj.status, obj.version
    res = s.execute(
        update(model)
        .where(model.id == obj_id, model.status == from_status, model.version == version)
        .values(status=t.to, version=version + 1)
        .execution_options(synchronize_session=False)
    )
    if res.rowcount != 1:
        raise ConcurrentUpdate(f"{entity} #{obj_id} 已被其他操作修改")
    s.refresh(obj)
    audit(s, actor.label, f"{entity}.{event}", f"{entity}:{obj_id}", {"from": from_status, "to": t.to, **(payload or {})})
    emit(s, f"{entity}.{t.to}", entity, obj_id, {"from": from_status, "event": event, "actor": actor.label, **(payload or {})})
    return obj
