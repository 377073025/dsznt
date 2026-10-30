"""拼多多商家后台「机会商品」读取（R10 市场数据，免费）：专用浏览器打开机会商品页，取页面自己请求回来的数据，只读。

- 用「拼多多商家后台」那个专用浏览器（platforms.yaml 的 pdd_backend），每天最多读一次，过同一个限速器
- 页面出现验证、风控提示或跳到登录页就停下：记为未登录或抛出风控异常，交给上层暂停并通知人工；不做任何绕过
- 后台只给本店主营类目；趋势由每天存的快照算：和 7 天前（最多往前找 35 天）比，新出现的词记为翻倍
- 竞品详情（competitor_goods）不支持
"""
from __future__ import annotations

import json
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable

from app.adapters.market_tool import pdd_chance_parse as P
from app.adapters.types import CompetitorGoods, KeywordStat, RankItem
from app.config import get_settings
from app.data.channels.web_reader import profiles
from app.data.channels.web_reader.browser import browser_lock
from app.data.channels.web_reader.limiter import get_limiter
from app.data.errors import CaptchaRequired, LoginExpired, RiskBlocked

log = logging.getLogger(__name__)
PLATFORM = "pdd_backend"
CHANCE_URL = "https://mms.pinduoduo.com/goods/goods_list/chance?activeTabKey=1"
HOT_PAGES = 3  # 热搜商品每页 10 个，读前 3 页
NEW_WORD_TREND = 1.0


def check_risk(url: str, text: str) -> None:
    kw = get_settings().platforms["risk_keywords"]
    head = text[:3000]
    if any(w in head for w in kw["captcha"]):
        raise CaptchaRequired(f"拼多多后台出现验证：{url[:80]}")
    if any(w in head for w in kw["blocked"]):
        raise RiskBlocked(f"拼多多后台出现风控提示：{head[:60]}")


class PddChanceWeb:
    channel = "web"
    platform = PLATFORM

    def __init__(self, lane: str = "main", opener: Callable[[], dict[str, Any]] | None = None,
                 today: Callable[[], date] = date.today, snapshot_dir: Path | None = None):
        self.lane, self.today = lane, today
        self._open = opener or self._open_page
        self._snapshot_dir = snapshot_dir
        self._cache: dict[str, dict[str, Any]] = {}  # 同一天只读一次（选品会对每个需求反复取）

    @property
    def snapshot_dir(self) -> Path:
        return self._snapshot_dir or get_settings().path("data_dir") / "market_snapshots" / "pdd_chance"

    def _open_page(self) -> dict[str, Any]:
        """打开机会商品页，返回 {url, filters, shortage, hot: [每页热搜商品]}。"""
        from playwright.sync_api import sync_playwright

        if profiles.window_open(PLATFORM):
            raise LoginExpired("拼多多后台的登录窗口还开着，请先关掉")
        got: dict[str, Any] = {"hot": []}

        def on_response(r) -> None:
            if "opportunity-goods/chance-goods/" not in r.url:
                return
            name = r.url.split("chance-goods/")[-1].split("?")[0]
            try:
                if name == "get_filters":
                    got["filters"] = r.json()
                elif name == "home_list" and '"banner_type":1' in (r.request.post_data or ""):
                    got.setdefault("shortage", r.json())  # 只要第一页
                elif name == "get_hot_search_goods":
                    got["hot"].append(r.json())
            except Exception:  # noqa: BLE001  个别返回不是 JSON 就跳过
                log.warning("机会商品返回解析失败：%s", name)

        with browser_lock(PLATFORM, self.lane), sync_playwright() as p:
            get_limiter(PLATFORM).acquire(self.lane)
            ctx = p.chromium.launch_persistent_context(
                str(profiles.profile_dir(PLATFORM)), executable_path=profiles.browser_path(), headless=True,
                viewport={"width": 1440, "height": 900})
            try:
                page = ctx.new_page()
                page.on("response", on_response)
                page.goto(CHANCE_URL, wait_until="domcontentloaded", timeout=45000)
                for _ in range(30):
                    if "/login" in page.url or ("filters" in got and "shortage" in got):
                        break
                    page.wait_for_timeout(1000)
                if "/login" in page.url:
                    return {"url": page.url}
                check_risk(page.url, page.inner_text("body"))
                page.get_by_text("热点运营", exact=True).first.click(timeout=10000)
                page.wait_for_timeout(4000)
                for _ in range(HOT_PAGES - 1):
                    nxt = page.locator("[data-testid='beast-core-pagination-next']")
                    if not nxt.count() or "disabled" in (nxt.first.get_attribute("class") or ""):
                        break
                    nxt.first.click(timeout=5000)  # 下一页
                    page.wait_for_timeout(3000)
                check_risk(page.url, page.inner_text("body"))
                return {"url": page.url, **got}
            finally:
                ctx.close()

    def _read(self) -> dict[str, Any]:
        key = str(self.today())  # 按天缓存，第二天重新读
        if key not in self._cache:
            got = self._open()
            if any(w in got["url"] for w in profiles.login_defs()[PLATFORM].logged_out):
                profiles.mark_logged_out(PLATFORM, got["url"])
                raise LoginExpired("拼多多后台登录已失效，请在「接入状态」里重新登录")
            if "filters" not in got or "shortage" not in got or not got["hot"]:
                raise P.PddChanceError("机会商品页没有读到完整数据（页面可能改版）")
            self._cache[key] = got
            self._save_raw(got)
        return self._cache[key]

    def _save_raw(self, got: dict[str, Any]) -> None:
        """原始数据留一份，页面改版时用来对比。"""
        d = self.snapshot_dir / "raw"
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{self.today()}.json").write_text(json.dumps(got, ensure_ascii=False), encoding="utf-8")

    def _with_trend(self, words: list[KeywordStat]) -> list[KeywordStat]:
        """把今天的热度存成快照，再和 7 天前的快照比出趋势；没有旧快照时趋势为 0。"""
        today = self.today()
        self.snapshot_dir.mkdir(parents=True, exist_ok=True)
        (self.snapshot_dir / f"{today}.json").write_text(
            json.dumps({w.keyword: w.heat for w in words}, ensure_ascii=False), encoding="utf-8")
        old = None
        for back in range(7, 36):
            f = self.snapshot_dir / f"{today - timedelta(days=back)}.json"
            if f.is_file():
                old = json.loads(f.read_text(encoding="utf-8"))
                break
        if old is None:
            return words
        out = []
        for w in words:
            before = old.get(w.keyword)
            trend = NEW_WORD_TREND if not before else round((w.heat - before) / before, 3)
            out.append(w.model_copy(update={"trend": trend}))
        return out

    def hot_keywords(self, category: str, week: str) -> list[KeywordStat]:
        got = self._read()
        words = self._with_trend(P.keywords(got["hot"], got["shortage"], got["filters"]))  # 快照存全部词
        return [w for w in words if P.related(w.keyword, category)]

    def category_ranking(self, category: str, week: str) -> list[RankItem]:
        return P.hot_goods(self._read()["hot"], category)

    def competitor_goods(self, goods_url: str) -> CompetitorGoods:
        raise LookupError("机会商品不支持按链接查竞品，请直接给商品名和售价")
