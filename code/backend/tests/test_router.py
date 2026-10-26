from app import capabilities as caps
from app.data import router
from app.data.errors import CaptchaRequired, RiskBlocked
from app.db import session_scope
from app.models import Capability, DomainEvent, RiskEvent, Shop
from app.workflow.jobs import dispatch_events
from tests.conftest import DAY


def _fetch(services, item="goods_snapshot"):
    with session_scope() as s:
        shop = s.query(Shop).first()
        job = router.fetch(s, services, shop, item, DAY)
        return job.status, job.channel


def test_api_not_configured_falls_back_to_web(env):
    services, _ = env
    assert _fetch(services) == ("success", "web")  # pdd_api 未申请下来 → 走网页


def test_api_used_when_healthy(env):
    services, _ = env
    with session_scope() as s:
        caps.set_status(s, "pdd_api", caps.HEALTHY)
    assert _fetch(services) == ("success", "api")


def test_web_unhealthy_requests_manual_and_notifies(env):
    services, _ = env
    with session_scope() as s:
        caps.set_status(s, "pdd_web", caps.DEGRADED)
    assert _fetch(services, "goods_daily_traffic") == ("manual_requested", "manual")
    dispatch_events()
    card = services.feishu.sent[-1]["card"]
    assert "请导出数据" in card["header"]["title"]["content"]


def test_captcha_pauses_platform(env, monkeypatch):
    services, _ = env

    def boom(*_a, **_k):
        raise CaptchaRequired("滑块")

    monkeypatch.setattr(services.pdd["web"], "goods_daily_traffic", boom)
    assert _fetch(services, "goods_daily_traffic") == ("waiting_human", "web")
    with session_scope() as s:
        assert s.get(Capability, "pdd_web").status == caps.BLOCKED
        assert s.query(RiskEvent).filter_by(type="captcha").count() == 1


def test_risk_blocked_opens_circuit(env, monkeypatch):
    services, _ = env

    def boom(*_a, **_k):
        raise RiskBlocked("访问受限")

    monkeypatch.setattr(services.pdd["web"], "list_goods", boom)
    assert _fetch(services) == ("circuit_open", "web")
    with session_scope() as s:
        assert s.query(DomainEvent).filter_by(type="fetch.circuit_open").count() == 1


def test_invalid_data_is_not_stored(env, monkeypatch):
    services, _ = env
    from app.adapters.types import GoodsTraffic

    bad = [GoodsTraffic(goods_id="g_x", day=DAY, visitors=10, add_to_cart=1, orders=50)]
    monkeypatch.setattr(services.pdd["web"], "goods_daily_traffic", lambda *_a: bad)
    status, _ = _fetch(services, "goods_daily_traffic")
    assert status == "manual_requested"  # 网页数据不合理 → 降到人工导入
    with session_scope() as s:
        from app.models import DailyMetric

        assert s.query(DailyMetric).count() == 0


def test_same_item_day_not_refetched(env):
    services, _ = env
    _fetch(services)
    with session_scope() as s:
        shop = s.query(Shop).first()
        job = router.fetch(s, services, shop, "goods_snapshot", DAY)
        assert job.attempts == 1
