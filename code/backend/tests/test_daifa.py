"""代发助手王读取：用保存的真实返回测解析；每天读一次；跳到登录页记为未登录；每日检查的提醒和货源回填。"""
import json
from datetime import date
from pathlib import Path

import pytest

from app.adapters.dropship_tool import daifa_parse as P
from app.adapters.dropship_tool.daifa_web import DaifaWeb
from app.data.channels.web_reader import profiles
from app.data.errors import LoginExpired

FIX = Path(__file__).parent / "fixtures" / "daifa"


def _got():
    return json.loads((FIX / "status.json").read_text(encoding="utf-8"))


def test_parse():
    g = _got()
    shop = P.pdd_shop(g["shops"])
    assert shop == {"id": "5323625", "name": "精选品质校服", "authorized": True, "auth_expire": "2026-12-10"}
    assert P.service_end(g["service"]) == "2026-12-02"
    linked = P.linked_goods(g["linked"])
    assert len(linked) == 6 and linked[0]["offer_id"] == "1688_901470516429" and linked[0]["source_price_fen"] == 1950
    unlinked = P.unlinked_onsale(g["items"])
    assert len(unlinked) == 7 and all(x["price_fen"] >= 100 for x in unlinked)  # 两个 0.01 元补差价链接不算
    with pytest.raises(P.DaifaDataError, match="参数有误"):
        P.pdd_shop({"code": 102, "message": "参数有误"})


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "_state_file", lambda p: tmp_path / f"{p}.login.json")
    (tmp_path / "alibaba.login.json").write_text('{"logged_in": true}', encoding="utf-8")
    return tmp_path


def _web(got=None, today=date(2026, 11, 27)):
    calls = []

    def opener():
        calls.append(1)
        return got or _got()

    return DaifaWeb(opener=opener, today=lambda: today), calls


def test_status_once_per_day_and_auto_order(state):
    web, calls = _web()
    st = web.status()
    assert st["orders"] == {"wait_purchase": 44, "wait_send": 0, "sent": 23, "refunding": 3}  # 只取拼多多店铺那份
    assert web.check_auto_order("1688_901470516429").supported is True
    assert web.check_auto_order("1688_123").supported is None
    assert calls == [1]
    from app.adapters.types import ListingPackage

    res = web.push_listing(ListingPackage(title="t", main_images=[], price_fen=100, source_offer_url="https://x/1.html"))
    assert not res.ok and "链接铺货" in res.message


def test_login_page_marks_logged_out(state):
    web, _ = _web({"url": "https://login.1688.com/member/signin.htm"})
    with pytest.raises(LoginExpired):
        web.status()
    assert profiles.login_state("alibaba")["logged_in"] is False
    assert web.check_auto_order("1688_1").supported is None  # 选品打分时读不到按未知处理


def test_daily_check_alerts_and_backfills(env, state):
    from sqlalchemy import select

    from app import capabilities as caps
    from app.db import session_scope
    from app.models import Product
    from app.workflow.on_sale import check_dropship

    services, _ = env
    services.dropship, _ = _web()
    with session_scope() as s:
        caps.set_status(s, "dropship_tool", caps.HEALTHY)
        s.add(Product(shop_id=1, goods_id="1012228825085", name="泳衣", title="泳衣", price_fen=2106))
        s.flush()
        issues = check_dropship(s, services, date(2026, 11, 27))
        assert any("44 笔订单待采购" in x for x in issues)
        assert any("2026-12-02 到期" in x for x in issues) and any("店铺授权 2026-12-10" in x for x in issues)
        assert any("7 个在售商品没关联货源" in x for x in issues)
        prod = s.scalar(select(Product).where(Product.goods_id == "1012228825085"))
        assert prod.source_offer_id == "1688_901470516429"  # 已关联的货源记到商品上，货源复查会接着看
        assert check_dropship(s, services, date(2026, 11, 27))  # 同一天再查不重复发卡片（返回问题照旧）
