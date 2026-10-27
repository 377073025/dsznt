"""手工货源：1688 读取没接通时，人照着商品页录入，单品评估、找货源（关键词）照常可用。"""
from datetime import datetime

from app import capabilities as caps
from app.adapters import fixtures
from app.db import session_scope
from app.models import Candidate, Offer
from app.workflow import adhoc
from app.workflow.jobs import run_until_idle
from tests.conftest import DAY, OWNER, backfill
from tests.test_console_api import H_OP, H_OWNER, _client
from tests.test_workbench import _submit, _task, _user

GRAPEFRUIT = {**fixtures._OFFERS["1688_6543210"].model_dump(exclude={"offer_id"}),
              "offer": "https://detail.1688.com/offer/6543210.html"}


def _no_1688():
    with session_scope() as s:
        for key in ("alibaba_api", "alibaba_web"):
            caps.set_status(s, key, caps.NOT_CONFIGURED)


def test_put_manual_offer_validates_and_lists(env):
    with _client() as c:
        assert c.put("/api/offers/manual", json=GRAPEFRUIT, headers={"X-Open-Id": "ou_v"}).status_code == 403
        assert c.put("/api/offers/manual", json={**GRAPEFRUIT, "offer": "abc"}, headers=H_OP).status_code == 400
        assert c.put("/api/offers/manual", json={**GRAPEFRUIT, "ship_48h_rate": 98}, headers=H_OP).status_code == 400
        r = c.put("/api/offers/manual", json=GRAPEFRUIT, headers=H_OP).json()
        assert (r["offer_id"], r["price_fen"], r["entered_by"]) == ("1688_6543210", 1180, "ou_operator")
        c.put("/api/offers/manual", json={**GRAPEFRUIT, "price_fen": 1250}, headers=H_OWNER)
        rows = c.get("/api/offers/manual", headers=H_OP).json()
        assert [(x["offer_id"], x["price_fen"], x["entered_by"]) for x in rows] == [("1688_6543210", 1250, OWNER)]


def test_evaluate_offer_falls_back_to_manual_offer(env, monkeypatch):
    services, gateway = env
    backfill(services, DAY, 1)
    _no_1688()
    monkeypatch.setattr(adhoc, "_now_cn", lambda: datetime(2026, 10, 24, 10, 0))
    with session_scope() as s:
        from app.agents.catalog import CATALOG, missing_capabilities

        assert missing_capabilities(s, CATALOG["selection.evaluate_offer"]) == ["alibaba_api/alibaba_web"]
    with _client() as c:
        c.put("/api/offers/manual", json=GRAPEFRUIT, headers=H_OP)
    tid = _submit("selection.evaluate_offer", {"offer": "1688_6543210", "sell_price_fen": 2990, "keyword": "葡萄柚香薰蜡烛",
                                               "trend": 0.34})
    missing = _submit("selection.evaluate_offer", {"offer": "1688_6543299", "sell_price_fen": 2990})
    found = _submit("selection.find_source", {"keyword": "葡萄柚 蜡烛", "sell_price_fen": 2990})
    run_until_idle(services, gateway)
    t = _task(tid)
    assert t.status == "done" and t.result["passed"] and "建议上新" in t.result["reason"]
    assert _task(missing).status == "failed" and "没有手工录入" in _task(missing).error
    assert [o["offer_id"] for o in _task(found).result["offers"]] == ["1688_6543210"]
    with session_scope() as s:
        adhoc.promote(s, services, _user(s, OWNER), tid)
        assert s.query(Candidate).one().offer_id == "1688_6543210"
        assert s.get(Offer, "1688_6543210").raw["manual"]  # 转候选重写货源快照时不丢手工标记


def test_weekly_selection_and_offer_watch_ignore_manual_offers(env):
    from app.agents import selection

    services, _ = env
    _no_1688()
    with _client() as c:
        c.put("/api/offers/manual", json=GRAPEFRUIT, headers=H_OP)
    with session_scope() as s:
        assert selection.sourcing(s, services) is None
        assert selection.sourcing(s, services, allow_manual=True).channel == "manual"
