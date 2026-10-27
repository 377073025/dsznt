"""飞书卡片回调（07 号文档 9.2 节）。

on_card_action 必须在 3 秒内返回：只做角色校验、幂等检查、入队；
真正的状态变化在 worker 里由 apply_card_action 完成，然后延时更新卡片。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.data import router
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import cards_for
from app.models import AdhocTask, CardMessage, FetchJob, ProcessedAction, Shop, Task, User
from app.services import Services
from app.workflow import adhoc
from app.workflow.events import audit
from app.workflow.jobs import JobContext, enqueue, job_handler
from app.workflow.state_machine import MODELS, Actor, TransitionDenied, can, transition

EVENT_LABELS = {
    "approve": "已同意", "reject": "已拒绝", "revise": "待修改，请直接回复修改意见", "confirm": "已确认写入",
    "cancel": "已取消写入", "keep": "已确认留款", "optimize": "已确认优化后再观察", "drop": "已确认下架",
    "promote": "已转化，请在新卡片上拍板",
}


def _toast(kind: str, text: str) -> dict[str, Any]:
    return {"toast": {"type": kind, "content": text}}


def get_or_register(s: Session, open_id: str) -> User:
    user = s.scalar(select(User).where(User.feishu_open_id == open_id))
    if user is None:
        user = User(feishu_open_id=open_id, role="viewer")
        s.add(user)
        s.flush()
    return user


def on_card_action(s: Session, open_id: str, value: dict[str, Any]) -> dict[str, Any]:
    user = get_or_register(s, open_id)
    entity, obj_id, event, version = value["entity"], int(value["id"]), value["event"], int(value["version"])
    if entity not in MODELS and entity not in ("fetch_job", "adhoc_task"):
        return _toast("error", "无法识别的操作")
    key = f"{entity}:{obj_id}:{event}:{version}"
    if s.get(ProcessedAction, key) is not None:
        return _toast("info", "这个操作已经处理过了")
    if entity == "fetch_job":
        job = s.get(FetchJob, obj_id)
        reason = router.resume_denied_reason(job, user.role) if job else "采集任务不存在"
        if reason:
            return _toast("error", reason)
    elif entity == "adhoc_task":
        t = s.get(AdhocTask, obj_id)
        if user.role not in adhoc.SUBMIT_ROLES:
            return _toast("error", "你没有这个操作的权限")
        if t is None or t.status != "done":
            return _toast("warning", "这个单独任务已经处理过了")
    else:
        obj = s.get(MODELS[entity], obj_id)
        actor = Actor("user", open_id, user.role)
        if obj is None or obj.version != version:
            return _toast("warning", "内容已更新，请看最新的卡片")
        if not can(entity, obj.status, event, actor):
            return _toast("error", "你没有这个操作的权限" if user.role != "owner" else f"当前状态 {obj.status} 不能执行该操作")
    s.add(ProcessedAction(key=key))
    enqueue(s, "card.apply", {"open_id": open_id, "entity": entity, "id": obj_id, "event": event}, dedupe_key=f"card:{key}")
    return _toast("info", "已收到，处理中")


def apply_action(s: Session, services: Services, open_id: str, entity: str, obj_id: int, event: str) -> None:
    """Web 控制台的同类操作也走这里，保证两边规则一致。"""
    user = get_or_register(s, open_id)
    actor = Actor("user", open_id, user.role)
    if entity == "fetch_job":
        job = s.get(FetchJob, obj_id)
        reason = router.resume_denied_reason(job, user.role)
        if reason:
            raise TransitionDenied(reason)
        audit(s, actor.label, "fetch.resume", f"fetch_job:{obj_id}", {"status": job.status})
        router.resume(s, services, s.scalar(select(Shop).order_by(Shop.id)), obj_id)
        _resolve_cards(s, services, entity, obj_id, "已恢复采集")
        return
    if entity == "adhoc_task":
        ref = adhoc.promote(s, services, user, obj_id)
        _resolve_cards(s, services, entity, obj_id, f"已转化为 {ref}，请在新卡片上拍板")
        return
    if entity == "task" and event == "confirm":
        task = s.get(Task, obj_id)
        if task is None or not can("task", task.status, "confirm", actor):
            raise TransitionDenied("无权确认写入")
        task.write_confirmed_by = open_id
    transition(s, entity, obj_id, event, actor)
    _resolve_cards(s, services, entity, obj_id, EVENT_LABELS.get(event, event))


def _resolve_cards(s: Session, services: Services, entity: str, obj_id: int, note: str) -> None:
    if services.feishu is not None:
        for cm in cards_for(s, entity, obj_id):
            new_card = cards.resolved(cm.card, entity, obj_id, note)
            services.feishu.update_card(cm.message_id, new_card)
            # 同一消息的其他登记也要用最新卡片，否则下次更新会把这次的结果覆盖掉
            for other in s.scalars(select(CardMessage).where(CardMessage.message_id == cm.message_id)):
                other.card = new_card
            cm.status = "resolved"


@job_handler("card.apply")
def _card_apply(ctx: JobContext, p: dict[str, Any]) -> None:
    apply_action(ctx.session, ctx.services, p["open_id"], p["entity"], p["id"], p["event"])
