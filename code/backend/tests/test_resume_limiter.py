from datetime import datetime, timedelta

import pytest

from app import capabilities as caps
from app.data.channels.web_reader.limiter import DailyLimitReached, RateLimiter
from app.data.errors import CaptchaRequired, RiskBlocked
from app.db import session_scope
from app.interfaces.feishu.handler import on_card_action
from app.models import Capability, FetchJob
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER


def _pause(services, gateway, monkeypatch, exc):
    calls = {"n": 0}
    real = services.pdd["web"].goods_daily_traffic

    def flaky(*a):
        calls["n"] += 1
        if calls["n"] == 1:
            raise exc
        return real(*a)

    monkeypatch.setattr(services.pdd["web"], "goods_daily_traffic", flaky)
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    with session_scope() as s:
        return s.query(FetchJob).filter_by(item="goods_daily_traffic").one()


def _resume_button(services):
    return next(a["value"] for m in services.feishu.sent if "card" in m
                for e in m["card"]["elements"] if e.get("tag") == "action" for a in e["actions"] if a["value"]["event"] == "resume")


def test_captcha_resume_by_operator(env, monkeypatch):
    services, gateway = env
    job = _pause(services, gateway, monkeypatch, CaptchaRequired("滑块"))
    assert job.status == "waiting_human"
    v = _resume_button(services)
    assert v["event"] == "resume"
    with session_scope() as s:
        assert on_card_action(s, "ou_stranger", v)["toast"]["content"] == "你没有恢复采集的权限"
        assert on_card_action(s, OPERATOR, v)["toast"]["content"] == "已收到，处理中"
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert s.get(Capability, "pdd_web").status == caps.HEALTHY
        # 暂停的那项，以及暂停期间被迫转人工的同平台数据项，都重新自动采集
        assert {j.item: j.status for j in s.query(FetchJob)} == {
            "goods_snapshot": "success", "goods_daily_traffic": "success", "order_stats": "success"}


def test_circuit_needs_owner_and_24h(env, monkeypatch):
    services, gateway = env
    job = _pause(services, gateway, monkeypatch, RiskBlocked("访问受限"))
    assert job.status == "circuit_open"
    v = _resume_button(services)
    with session_scope() as s:
        assert "只能由店主" in on_card_action(s, OPERATOR, v)["toast"]["content"]
        assert "未满 24 小时" in on_card_action(s, OWNER, v)["toast"]["content"]
        s.get(FetchJob, job.id).updated_at -= timedelta(hours=25)
    with session_scope() as s:
        # updated_at 有 onupdate，直接用 SQL 改回 25 小时前
        from sqlalchemy import update

        s.execute(update(FetchJob).where(FetchJob.id == job.id).values(updated_at=datetime.utcnow() - timedelta(hours=25)))
    with session_scope() as s:
        assert on_card_action(s, OWNER, v)["toast"]["content"] == "已收到，处理中"
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert s.get(FetchJob, job.id).status == "success"


def test_rate_limiter_spacing_and_daily_cap():
    slept = []
    now = [datetime(2026, 10, 24, 9, 0)]
    lim = RateLimiter("pdd_front", sleep=slept.append, clock=lambda: now[0])
    lim.acquire()
    now[0] += timedelta(seconds=10)
    lim.acquire()
    assert slept and 49 <= slept[0] <= 51  # 平台间隔 60 秒，已过 10 秒
    for _ in range(18):
        lim.acquire()
    with pytest.raises(DailyLimitReached):
        lim.acquire()  # pdd_front 每日 20 页
    now[0] += timedelta(days=1)
    lim.acquire()  # 跨天清零
    assert lim.used_today == 1
