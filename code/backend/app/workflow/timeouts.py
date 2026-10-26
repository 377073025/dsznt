"""超时与提醒（07 号文档 8.3 节），worker 每 30 分钟跑一次 system.timeouts。

| 等待状态                | 提醒                 | 超时处理                              |
| 建议 reported/revising  | 当天 20:00 后提醒一次 | 48 小时后过期，不执行                  |
| 写入确认                | 2 小时后提醒一次      | 24 小时后取消写入，任务回到 produced    |
| 人工导入 manual_requested | 30 分钟后提醒一次     | 到截止时间标记缺失，下游带着缺失继续出早报 |
（人工导入的截止时间是数据项的 deadline_minutes，默认 90 分钟，所以提醒不能按 2 小时算。）
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data import router
from app.data.items import load_items
from app.db import utcnow
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import send_card
from app.models import FetchJob, ProcessedAction, Suggestion, Task
from app.services import Services
from app.workflow.events import emit
from app.workflow.jobs import JobContext, job_handler
from app.workflow.state_machine import SYSTEM, transition

REMIND_AFTER = timedelta(hours=2)
EVENING_REMIND_HOUR = 20


def _once(s: Session, key: str) -> bool:
    """同一个提醒只发一次。"""
    if s.get(ProcessedAction, key) is not None:
        return False
    s.add(ProcessedAction(key=key))
    return True


def _remind(s: Session, services: Services, title: str, body: str, entity: str, entity_id: int) -> None:
    send_card(s, services, cards.need_human(title, body), entity, entity_id)


def run(s: Session, services: Services, now: datetime | None = None) -> dict[str, int]:
    now = now or utcnow()
    conf = get_settings().raw["timeouts"]
    stats = {"expired": 0, "write_cancelled": 0, "missing": 0, "reminded": 0}

    # 建议：48 小时未拍板过期；北京时间 20:00 后提醒一次
    pending = s.scalars(select(Suggestion).where(Suggestion.status.in_(("reported", "revising")))).all()
    for x in pending:
        if now - x.updated_at >= timedelta(hours=conf["suggestion_expire_hours"]):
            transition(s, "suggestion", x.id, "expire", SYSTEM)
            stats["expired"] += 1
        elif (now + timedelta(hours=8)).hour >= EVENING_REMIND_HOUR and _once(s, f"remind:suggestion:{x.id}"):
            _remind(s, services, "建议待拍板", f"建议 #{x.id}「{x.action}」还没有拍板，48 小时后自动过期。", "suggestion", x.id)
            stats["reminded"] += 1

    # 写入确认：2 小时提醒，24 小时取消
    for t in s.scalars(select(Task).where(Task.status == "awaiting_write_confirm")).all():
        waited = now - t.updated_at
        if waited >= timedelta(hours=conf["write_confirm_expire_hours"]):
            transition(s, "task", t.id, "cancel", SYSTEM, {"reason": "写入确认超时"})
            stats["write_cancelled"] += 1
        elif waited >= REMIND_AFTER and _once(s, f"remind:task:{t.id}"):
            _remind(s, services, "写入待确认", f"任务 #{t.id} 已等待写入确认 {int(waited.total_seconds() // 3600)} 小时，"
                    f"{conf['write_confirm_expire_hours']} 小时后自动取消。", "task", t.id)
            stats["reminded"] += 1

    # 人工导入：2 小时提醒，到截止时间标记缺失
    items = load_items()
    for j in s.scalars(select(FetchJob).where(FetchJob.status == router.MANUAL_REQUESTED)).all():
        waited = now - (j.started_at or j.created_at)
        if waited >= timedelta(minutes=items[j.item].deadline_minutes):
            j.status, j.finished_at = router.MISSING, now
            emit(s, "fetch.missing", "fetch_job", j.id, {"item": j.item, "day": j.day.isoformat()})
            stats["missing"] += 1
        elif waited >= timedelta(minutes=conf["manual_import_remind_minutes"]) and _once(s, f"remind:fetch:{j.id}"):
            _remind(s, services, "数据还没收到", f"{j.day} 的 {j.item} 还在等你导出，超过截止时间后今天的分析会跳过这部分。",
                    "fetch_job", j.id)
            stats["reminded"] += 1
    s.flush()
    return stats


@job_handler("system.timeouts")
def _timeouts(ctx: JobContext, _p: dict) -> None:
    run(ctx.session, ctx.services)
