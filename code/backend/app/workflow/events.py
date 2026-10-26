"""领域事件 outbox：写 domain_event 表，worker 轮询分发给订阅者。"""
from __future__ import annotations

import logging
from collections import defaultdict
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AuditLog, DomainEvent

log = logging.getLogger(__name__)

Handler = Callable[[Session, DomainEvent], None]
_handlers: dict[str, list[Handler]] = defaultdict(list)


def emit(s: Session, type_: str, entity: str, entity_id: int, payload: dict[str, Any] | None = None) -> None:
    s.add(DomainEvent(type=type_, entity=entity, entity_id=entity_id, payload=payload or {}))


def audit(s: Session, actor: str, action: str, target: str, payload: dict[str, Any] | None = None) -> None:
    s.add(AuditLog(actor=actor, action=action, target=target, payload=payload or {}))


def subscribe(type_: str) -> Callable[[Handler], Handler]:
    def deco(fn: Handler) -> Handler:
        _handlers[type_].append(fn)
        return fn

    return deco


def dispatch_pending(s: Session, limit: int = 100) -> int:
    """分发未处理的事件；处理器抛错时记录日志，事件仍标记为已分发，避免死循环。"""
    events = s.scalars(
        select(DomainEvent).where(DomainEvent.dispatched.is_(False)).order_by(DomainEvent.id).limit(limit)
    ).all()
    for ev in events:
        ev.dispatched = True
        for h in _handlers.get(ev.type, []):
            try:
                with s.begin_nested():
                    h(s, ev)
            except Exception:
                log.exception("event handler failed: %s #%s", ev.type, ev.id)
        s.flush()
    return len(events)
