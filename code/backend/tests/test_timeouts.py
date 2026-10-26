from datetime import datetime, timedelta

from app import capabilities as caps
from app.data import router
from app.db import session_scope, utcnow
from app.models import FetchJob, Job, Shop, Suggestion, Task
from app.workflow import timeouts
from app.workflow.jobs import dispatch_events, enqueue
from tests.conftest import DAY


def _sug(s, status="reported"):
    sug = Suggestion(shop_id=s.query(Shop).first().id, day=DAY, type="other", action="a", reason="r", status=status)
    s.add(sug)
    s.flush()
    return sug


def test_suggestion_expires_after_48h(env):
    services, _ = env
    with session_scope() as s:
        sid = _sug(s).id
    with session_scope() as s:
        assert timeouts.run(s, services, now=utcnow() + timedelta(hours=47))["expired"] == 0
        assert timeouts.run(s, services, now=utcnow() + timedelta(hours=49))["expired"] == 1
        assert s.get(Suggestion, sid).status == "expired"


def test_evening_reminder_once(env):
    services, _ = env
    with session_scope() as s:
        _sug(s)
    evening = datetime(2026, 10, 24, 12, 30) + timedelta(seconds=0)  # 北京时间 20:30
    with session_scope() as s:
        s.query(Suggestion).update({"updated_at": evening - timedelta(hours=3)})
        assert timeouts.run(s, services, now=evening)["reminded"] == 1
        assert timeouts.run(s, services, now=evening + timedelta(minutes=30))["reminded"] == 0


def test_write_confirm_remind_then_cancel(env):
    services, _ = env
    with session_scope() as s:
        t = Task(ref_type="suggestion", ref_id=1, executor="image", spec={}, needs_write=True, status="awaiting_write_confirm")
        s.add(t)
        s.flush()
        tid = t.id
    with session_scope() as s:
        assert timeouts.run(s, services, now=utcnow() + timedelta(hours=3))["reminded"] == 1
        assert timeouts.run(s, services, now=utcnow() + timedelta(hours=25))["write_cancelled"] == 1
        assert s.get(Task, tid).status == "produced"


def test_manual_import_deadline_marks_missing_and_still_reports(env):
    services, gateway = env
    with session_scope() as s:
        caps.set_status(s, "pdd_web", caps.DEGRADED)  # 拼多多网页不可用 → 全部要人工导入
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    from app.workflow.jobs import run_until_idle

    run_until_idle(services, gateway)
    with session_scope() as s:
        assert {j.status for j in s.query(FetchJob)} == {router.MANUAL_REQUESTED}
        stats = timeouts.run(s, services, now=utcnow() + timedelta(minutes=40))
        assert stats["reminded"] == 3 and stats["missing"] == 0
        assert timeouts.run(s, services, now=utcnow() + timedelta(minutes=91))["missing"] == 3
    dispatch_events()
    with session_scope() as s:
        assert s.query(Job).filter_by(type="daily.analyze").count() == 1  # 缺失也照常出早报（注明缺失）
