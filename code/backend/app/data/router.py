"""通道路由（07 号文档 6.2、6.5 节）：按数据项配置依次尝试 API → 网页 → 人工导入。

- 跳过能力未 healthy 的通道；某通道失败或数据不合理 → 降到下一个通道
- 验证码 / 登录失效 → 平台暂停（waiting_human），人工处理后恢复
- 访问受限 → 熔断（circuit_open）≥ 24 小时，只能店主确认恢复
- 到人工通道 → 发「请导出数据」卡片，等文件（manual_requested）
- 成功入库后发 data.ready 事件，由编排决定下游作业
"""
from __future__ import annotations

import logging
from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import capabilities as caps
from app.data import store
from app.data.errors import CaptchaRequired, FetchError, LoginExpired, RiskBlocked
from app.data.items import ChannelDef, ItemDef, load_items
from app.db import utcnow
from app.models import FetchJob, RiskEvent, Shop
from app.services import Services
from app.workflow.events import emit

log = logging.getLogger(__name__)

SCHEDULED, RUNNING, SUCCESS = "scheduled", "running", "success"
MANUAL_REQUESTED, WAITING_HUMAN, CIRCUIT_OPEN = "manual_requested", "waiting_human", "circuit_open"
MISSING, FAILED = "missing", "failed"

# 这些状态下不自动重试，等人工（或截止时间）处理；force=True 时才重新采集
_SETTLED = {SUCCESS, MANUAL_REQUESTED, WAITING_HUMAN, CIRCUIT_OPEN, MISSING}
CIRCUIT_MIN_HOURS = 24


def _get_job(s: Session, item: str, day: date) -> FetchJob:
    job = s.scalar(select(FetchJob).where(FetchJob.item == item, FetchJob.day == day))
    if job is None:
        job = FetchJob(item=item, day=day, status=SCHEDULED)
        s.add(job)
        s.flush()
    return job


def _call(services: Services, ch: ChannelDef, shop: Shop, day: date):
    namespace, method = ch.call.split(".", 1)
    impl = (getattr(services, namespace) or {}).get(ch.type)
    if impl is None:
        return None
    return getattr(impl, method)(shop.platform_shop_id, day)


def fetch(s: Session, services: Services, shop: Shop, item: str, day: date, force: bool = False) -> FetchJob:
    idef = load_items()[item]
    job = _get_job(s, item, day)
    if job.status in _SETTLED and not force:
        return job
    job.status, job.attempts, job.error = RUNNING, job.attempts + 1, ""
    job.started_at = utcnow()
    errors = []
    for ch in idef.channels:
        if ch.type == "manual":
            _request_manual(s, job, ch, "；".join(errors))
            return job
        if not caps.is_healthy(s, ch.capability):
            errors.append(f"{ch.type}：{ch.capability} 不可用")
            continue
        try:
            raw = _call(services, ch, shop, day)
            if raw is None:
                errors.append(f"{ch.type}：未接入")
                continue
            records = store.normalize(idef, raw)
            store.validate(idef, records)
            job.rows = store.save(s, shop, idef, day, records, ch.type)
        except (CaptchaRequired, LoginExpired) as e:
            _pause(s, job, ch, "captcha" if isinstance(e, CaptchaRequired) else "login_expired", str(e))
            return job
        except RiskBlocked as e:
            _open_circuit(s, job, ch, str(e))
            return job
        except FetchError as e:
            log.warning("fetch %s %s via %s failed: %s", item, day, ch.type, e)
            errors.append(f"{ch.type}：{e}")
            continue
        except Exception as e:  # noqa: BLE001  适配器的任何错误都降到下一个通道
            log.exception("fetch %s %s via %s crashed", item, day, ch.type)
            errors.append(f"{ch.type}：{type(e).__name__}: {e}")
            continue
        job.status, job.channel, job.finished_at = SUCCESS, ch.type, utcnow()
        emit(s, "data.ready", "fetch_job", job.id, {"item": item, "day": day.isoformat(), "channel": ch.type})
        s.flush()
        return job
    job.status, job.error, job.finished_at = FAILED, "；".join(errors)[:2000], utcnow()
    s.flush()
    return job


def _request_manual(s: Session, job: FetchJob, ch: ChannelDef, error: str) -> None:
    job.status, job.channel, job.error = MANUAL_REQUESTED, "manual", error[:2000]
    s.flush()
    emit(s, "fetch.manual_requested", "fetch_job", job.id,
         {"item": job.item, "day": job.day.isoformat(), "template": ch.template, "guide": ch.guide})


def _risk_event(s: Session, ch: ChannelDef, kind: str) -> None:
    hour_ago = utcnow() - timedelta(hours=1)
    rate = s.scalar(select(func.count(FetchJob.id)).where(FetchJob.started_at >= hour_ago)) or 0
    s.add(RiskEvent(platform=ch.capability, page=ch.script or ch.call, type=kind, recent_rate=float(rate)))


def _pause(s: Session, job: FetchJob, ch: ChannelDef, kind: str, error: str) -> None:
    job.status, job.channel, job.error = WAITING_HUMAN, ch.type, error[:2000]
    caps.set_status(s, ch.capability, caps.BLOCKED, f"{kind}：{error}")
    _risk_event(s, ch, kind)
    s.flush()
    emit(s, "fetch.waiting_human", "fetch_job", job.id,
         {"item": job.item, "day": job.day.isoformat(), "kind": kind, "capability": ch.capability})


def _open_circuit(s: Session, job: FetchJob, ch: ChannelDef, error: str) -> None:
    job.status, job.channel, job.error = CIRCUIT_OPEN, ch.type, error[:2000]
    caps.set_status(s, ch.capability, caps.BLOCKED, f"熔断：{error}")
    _risk_event(s, ch, "blocked")
    s.flush()
    emit(s, "fetch.circuit_open", "fetch_job", job.id,
         {"item": job.item, "day": job.day.isoformat(), "capability": ch.capability})


def all_ready(s: Session, items: list[str], day: date) -> bool:
    jobs = s.scalars(select(FetchJob).where(FetchJob.day == day, FetchJob.item.in_(items))).all()
    return len(jobs) == len(items) and all(j.status == SUCCESS for j in jobs)


# ---------- 恢复 ----------

def _paused_capability(job: FetchJob) -> str:
    idef: ItemDef = load_items()[job.item]
    return next((c.capability for c in idef.channels if c.type == job.channel and c.capability), "")


def resume_denied_reason(job: FetchJob, role: str) -> str | None:
    """返回不能恢复的原因；可以恢复返回 None。验证码类运营即可恢复，熔断必须店主且满 24 小时。"""
    if job.status not in (WAITING_HUMAN, CIRCUIT_OPEN):
        return "这项采集不需要恢复"
    if role not in ("owner", "operator"):
        return "你没有恢复采集的权限"
    if job.status == CIRCUIT_OPEN:
        if role != "owner":
            return "熔断只能由店主确认恢复"
        if utcnow() - job.updated_at < timedelta(hours=CIRCUIT_MIN_HOURS):
            return f"熔断未满 {CIRCUIT_MIN_HOURS} 小时，暂不能恢复"
    return None


def resume(s: Session, services: Services, shop: Shop, job_id: int) -> FetchJob:
    """恢复平台并重新采集：暂停的那项，以及暂停期间因该平台不可用而被迫转人工的数据项。"""
    job = s.get(FetchJob, job_id)
    assert job is not None
    cap = _paused_capability(job)
    if cap:
        caps.set_status(s, cap, caps.HEALTHY, "人工确认恢复")
    fetch(s, services, shop, job.item, job.day, force=True)
    items = load_items()
    waiting = s.scalars(select(FetchJob).where(FetchJob.status == MANUAL_REQUESTED, FetchJob.id != job.id)).all()
    for other in waiting:
        if cap in items[other.item].capabilities():
            fetch(s, services, shop, other.item, other.day, force=True)
    return job
