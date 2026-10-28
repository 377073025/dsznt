"""1688 网页读取：用保存下来的真实页面测解析；遇到验证 / 登录页立刻停下并记为未登录。"""
import json
from pathlib import Path

import pytest

from app.adapters.alibaba1688 import web_parse
from app.adapters.alibaba1688.web import Alibaba1688Web
from app.adapters.types import OfferFilter
from app.data.channels.web_reader import profiles
from app.data.errors import CaptchaRequired, LoginExpired
from app.rules import selection as R

FIX = Path(__file__).parent / "fixtures" / "alibaba1688"


def _detail(offer: str):
    title, text = (FIX / f"detail_{offer}.txt").read_text(encoding="utf-8").split("\n", 1)
    imgs = FIX / f"detail_{offer}.imgs.html"
    return web_parse.parse_detail(f"1688_{offer}", title, text, imgs.read_text(encoding="utf-8") if imgs.exists() else "")


def test_search_links():
    links = json.loads((FIX / "search_links.json").read_text(encoding="utf-8"))
    briefs = web_parse.parse_search(links, limit=5)
    assert briefs[0].offer_id == "1688_820571818249" and briefs[0].title.startswith("丑橘香薰蜡烛")
    assert briefs[0].price_fen == 90 and len(briefs) == 5 and len({b.offer_id for b in briefs}) == 5


def test_detail_with_dropship_stats():
    d = _detail("820571818249")
    assert d.title == "丑橘香薰蜡烛批发candle香氛伴手礼物手工仿真水果造型橘子蜡烛"
    assert d.price_fen == 467 and d.shipping_fen == 0  # 一件代发包邮价
    assert d.dropship and d.stock == 766187 + 872018
    assert d.ship_48h_rate == 1.0 and d.repurchase_rate == pytest.approx(0.638) and d.supplier_years == 3
    assert d.weight_kg == pytest.approx(0.16) and d.longest_side_cm == 6.8 and d.category == "香薰蜡烛"


def test_detail_without_dropship_stats_uses_fulfilment_rate():
    d = _detail("1017225512488")
    assert d.price_fen == 275 and d.ship_48h_rate == 1.0 and d.repurchase_rate == pytest.approx(0.744)
    assert len(d.image_urls) >= 3 and all(u.startswith("https://cbu01.alicdn.com/img/ibank/") and u.endswith(".jpg") and "!!2217674806235-" in u
                                          for u in d.image_urls)
    # 解析结果能直接进硬性过滤（售价 19.9 元）
    assert R.hard_filter(d, 1990, _rules()["hard_filters"], _rules()["profit"]) == []


def _rules():
    from app.config import get_settings

    return get_settings().rules["selection"]


def _page(url, text, links=()):
    return lambda _url: {"url": url, "title": "x - 阿里巴巴", "text": text, "html": "", "links": list(links)}


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "_state_file", lambda p: tmp_path / f"{p}.login.json")
    (tmp_path / "alibaba.login.json").write_text('{"logged_in": true}', encoding="utf-8")
    return tmp_path


def test_search_through_adapter(state):
    links = json.loads((FIX / "search_links.json").read_text(encoding="utf-8"))
    web = Alibaba1688Web(opener=_page("https://s.1688.com/selloffer/offer_search.htm", "香薰蜡烛 找货源", links))
    assert len(web.search_by_keyword("香薰蜡烛", OfferFilter(max_price_fen=300), limit=3)) == 3
    assert web.search_by_image(Path("x.jpg"), 5) == []


@pytest.mark.parametrize("url,text,err", [
    ("https://s.1688.com/x", "请拖动滑块完成验证", CaptchaRequired),
    ("https://login.taobao.com/?redirect_url=x", "登录", LoginExpired),
])
def test_stops_on_captcha_or_login(state, url, text, err):
    web = Alibaba1688Web(opener=_page(url, text))
    with pytest.raises(err):
        web.offer_detail("1688_1")
    assert profiles.login_state("alibaba")["logged_in"] is False  # 记为未登录，等人工处理
    with pytest.raises(profiles.BrowserError):
        profiles.require_logged_in("alibaba")


def test_browser_lock_reentrant_in_same_thread():
    """单独任务先拿平台锁，适配器读页时再拿同一把锁，不能锁死。"""
    from app.data.channels.web_reader.browser import browser_lock

    with browser_lock("alibaba", lane="adhoc"), browser_lock("alibaba", lane="adhoc"):
        pass
