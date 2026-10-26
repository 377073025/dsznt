from datetime import date

import pytest

from app.db import session_scope
from app.models import Shop, Suggestion
from app.workflow.state_machine import Actor, ConcurrentUpdate, TransitionDenied, transition


def _reported(s) -> int:
    shop = s.query(Shop).first()
    sug = Suggestion(shop_id=shop.id, day=date(2026, 10, 23), type="main_image", action="a", reason="r", status="reported")
    s.add(sug)
    s.flush()
    return sug.id


def test_agent_cannot_approve(env):
    with session_scope() as s:
        sid = _reported(s)
        with pytest.raises(TransitionDenied):
            transition(s, "suggestion", sid, "approve", Actor("agent", "ceo"))


def test_operator_and_viewer_cannot_approve(env):
    with session_scope() as s:
        sid = _reported(s)
        for role in ("operator", "viewer"):
            with pytest.raises(TransitionDenied):
                transition(s, "suggestion", sid, "approve", Actor("user", "u", role))


def test_owner_approves_and_audit_event_written(env):
    from app.models import AuditLog, DomainEvent

    with session_scope() as s:
        sid = _reported(s)
        obj = transition(s, "suggestion", sid, "approve", Actor("user", "ou_owner", "owner"))
        assert obj.status == "approved" and obj.version == 2
        assert s.query(AuditLog).filter_by(action="suggestion.approve").count() == 1
        assert s.query(DomainEvent).filter_by(type="suggestion.approved").count() == 1


def test_invalid_transition_denied(env):
    with session_scope() as s:
        sid = _reported(s)
        with pytest.raises(TransitionDenied):
            transition(s, "suggestion", sid, "complete", Actor("system", "system"))


def test_optimistic_lock(env):
    from sqlalchemy import update

    with session_scope() as s:
        sid = _reported(s)
    with pytest.raises(ConcurrentUpdate):
        with session_scope() as s:
            stale = s.get(Suggestion, sid)  # 会话里持有 version=1 的对象（保留引用，避免被回收后重新加载）
            assert stale.version == 1
            # 模拟另一个操作已经改过这条记录（不同步到当前会话）
            s.execute(update(Suggestion).where(Suggestion.id == sid).values(version=5)
                      .execution_options(synchronize_session=False))
            transition(s, "suggestion", sid, "approve", Actor("user", "ou_owner", "owner"))
