"""电霸读取：用试用账号保存的真实数据测解析；数据太旧不用；跳到登录页记为未登录。"""
import json
from datetime import date
from pathlib import Path

import pytest

from app.adapters.market_tool import dianba_parse
from app.adapters.market_tool.dianba_web import HOT_GOODS, HOT_WORDS, DianbaWeb
from app.data.channels.web_reader import profiles
from app.data.errors import LoginExpired

FIX = Path(__file__).parent / "fixtures" / "dianba"


def _body(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))["body"]


def test_hot_words_filter_by_category():
    words, day = dianba_parse.hot_words(_body("hot_words"), "保温杯")
    assert day == "2024-11-05" and [w.keyword for w in words] == ["保温杯"]
    w = words[0]
    assert w.heat == 104812 and w.trend == pytest.approx(-0.2429) and 0 < w.competition <= 1
    everything, _ = dianba_parse.hot_words(_body("hot_words"))
    assert len(everything) == 40 and {"泡面", "泡面碗"} <= {x.keyword for x in everything}
    assert max(x.competition for x in everything) == 1.0


def test_hot_goods_ranking():
    goods, day = dianba_parse.hot_goods(_body("hot_goods"), "收纳袋")
    assert day == "2026-10-29" and goods and all("收纳袋" in g.title for g in goods)
    assert goods[0].rank == 1 and goods[0].sales_30d >= goods[-1].sales_30d and goods[0].price_fen > 0
    assert goods[0].image_url.startswith("http")
    # 标题里都没有这个词时退回全部热销
    assert len(dianba_parse.hot_goods(_body("hot_goods"), "香薰蜡烛")[0]) == 30


def test_error_response():
    with pytest.raises(dianba_parse.DianbaDataError, match="无权限"):
        dianba_parse.hot_words({"error_code": 403, "message": "无权限"})


def _web(state, url="https://pdd.dianba6.com/x", today=date(2026, 10, 30)):
    pages = {HOT_WORDS[0]: _body("hot_words"), HOT_GOODS[0]: _body("hot_goods")}
    calls = []

    def opener(path, api):
        calls.append(path)
        return {"url": url, "data": pages[path]}

    return DianbaWeb(opener=opener, today=lambda: today), calls


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "_state_file", lambda p: tmp_path / f"{p}.login.json")
    (tmp_path / "dianba.login.json").write_text('{"logged_in": true}', encoding="utf-8")
    return tmp_path


def test_adapter_reads_once_per_day_and_rejects_stale(state):
    web, calls = _web(state)
    assert len(web.category_ranking("收纳袋", "2026-W44")) >= 1
    web.category_ranking("垃圾袋", "2026-W44")
    assert calls == [HOT_GOODS[0]]  # 同一天只读一次
    with pytest.raises(dianba_parse.DianbaDataError, match="2024-11-05"):
        web.hot_keywords("保温杯", "2026-W44")  # 试用账号的热词是两年前的样本


def test_login_page_marks_logged_out(state):
    web, _ = _web(state, url="https://pdd.dianba6.com/login")
    with pytest.raises(LoginExpired):
        web.category_ranking("收纳袋", "2026-W44")
    assert profiles.login_state("dianba")["logged_in"] is False
