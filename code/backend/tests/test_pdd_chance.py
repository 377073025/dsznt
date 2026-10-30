"""拼多多后台「机会商品」读取：用保存的真实返回测解析；按天读一次、快照算趋势；跳到登录页记为未登录；缺货词进需求。"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from app.adapters.market_tool import pdd_chance_parse as P
from app.adapters.market_tool.pdd_chance_web import PddChanceWeb
from app.data.channels.web_reader import profiles
from app.data.errors import LoginExpired

FIX = Path(__file__).parent / "fixtures" / "pdd_chance"


def _load(name):
    return json.loads((FIX / f"{name}.json").read_text(encoding="utf-8"))


def _got(url="https://mms.pinduoduo.com/goods/goods_list/chance"):
    return {"url": url, "filters": _load("filters"), "shortage": _load("shortage"), "hot": [_load("hot_page1")]}


def test_band():
    assert P.band("500+") == 500 and P.band("10w+") == 100000 and P.band("10000+") == 10000
    assert P.band("5000～10000") == 7500 and P.band("500 ~ 1000") == 750 and P.band(None) == 0


def test_hot_goods_sorted_and_filtered():
    goods = P.hot_goods([_load("hot_page1")] * 2)  # 重复的页去重
    assert len(goods) == 10 and goods[0].rank == 1 and goods[0].sales_30d == 500
    assert goods[0].price_fen > 0 and goods[0].image_url.startswith("http")
    coats = P.hot_goods([_load("hot_page1")], "外套")  # 标题或相关搜索词里有「外套」的
    assert 0 < len(coats) < 10 and [g.rank for g in coats] == list(range(1, len(coats) + 1))
    assert len(P.hot_goods([_load("hot_page1")], "香薰蜡烛")) == 10  # 一个都不沾边时返回全部


def test_keywords():
    words = {w.keyword: w for w in P.keywords([_load("hot_page1")], _load("shortage"), _load("filters"))}
    hot = words["男童外套加绒"]
    assert not hot.shortage and hot.heat == 750 and hot.competition == P.HOT_COMPETITION
    short = words["校服冬装同款小学加绒"]
    assert short.shortage and short.heat == 100 * P.HOT_INDEX_SCALE and short.competition == P.SHORTAGE_COMPETITION
    assert words["羊羔绒马甲"].shortage  # 顾客在搜的词去掉前面的「.」
    assert "l" not in words  # 单个字母的噪声词不要
    only = P.keywords([_load("hot_page1")], _load("shortage"), _load("filters"), "校服")
    assert only and all("校服" in w.keyword for w in only)


def test_error_response():
    with pytest.raises(P.PddChanceError, match="无权限"):
        P.hot_goods([{"success": False, "error_msg": "无权限"}])


@pytest.fixture
def state(monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "_state_file", lambda p: tmp_path / f"{p}.login.json")
    (tmp_path / "pdd_backend.login.json").write_text('{"logged_in": true}', encoding="utf-8")
    return tmp_path


def _web(tmp_path, today, got=None):
    calls = []

    def opener():
        calls.append(1)
        return got or _got()

    return PddChanceWeb(opener=opener, today=lambda: today, snapshot_dir=tmp_path / "snap"), calls


def test_reads_once_per_day_and_trend_from_snapshot(state, tmp_path):
    day = date(2026, 11, 2)
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / f"{day - timedelta(days=7)}.json").write_text(json.dumps({"男童外套加绒": 500}), encoding="utf-8")
    web, calls = _web(tmp_path, day)
    words = {w.keyword: w for w in web.hot_keywords("", "2026-W45")}
    web.category_ranking("校服", "2026-W45")
    assert calls == [1]  # 同一天只读一次
    assert words["男童外套加绒"].trend == 0.5  # 750 比 500
    assert words["校服冬装同款小学加绒"].trend == 1.0  # 上周没有的词记为翻倍
    assert (snap / f"{day}.json").is_file() and (snap / "raw" / f"{day}.json").is_file()
    assert all("校服" in w.keyword for w in web.hot_keywords("校服", "2026-W45"))


def test_no_old_snapshot_trend_zero(state, tmp_path):
    web, _ = _web(tmp_path, date(2026, 11, 2))
    assert {w.trend for w in web.hot_keywords("", "2026-W45")} == {0.0}


def test_login_page_marks_logged_out(state, tmp_path):
    web, _ = _web(tmp_path, date(2026, 11, 2), got={"url": "https://mms.pinduoduo.com/login/?redirectUrl=x"})
    with pytest.raises(LoginExpired):
        web.category_ranking("校服", "2026-W45")
    assert profiles.login_state("pdd_backend")["logged_in"] is False


def test_incomplete_page_is_error(state, tmp_path):
    web, _ = _web(tmp_path, date(2026, 11, 2), got={"url": "https://mms.pinduoduo.com/goods", "hot": []})
    with pytest.raises(P.PddChanceError, match="改版"):
        web.category_ranking("校服", "2026-W45")


def test_shortage_words_become_demands_without_trend(env, state, tmp_path):
    from app.agents.selection import find_demands
    from app.db import session_scope

    services, _ = env
    services.market, _ = _web(tmp_path, date(2026, 11, 2))  # 没有旧快照：趋势全是 0
    with session_scope() as s:
        demands = {d.keyword: d for d in find_demands(s, services, "2026-W45", "校服")}
    assert "校服冬装同款小学加绒" in demands and demands["校服冬装同款小学加绒"].competitors
    assert all("校服" in k for k in demands)
