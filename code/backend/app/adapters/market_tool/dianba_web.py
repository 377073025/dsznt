"""电霸网页读取（R10 市场数据工具）：专用浏览器打开电霸页面，取页面自己请求回来的数据（JSON），只读。

- 和 1688 一样：每页先过限速器（platforms.yaml 的 dianba.rate_limit），同一时刻只有一个任务用这个浏览器
- 跳到登录页就停下并记为未登录；数据日期太旧（max_data_age_days）不用
- 竞品详情（competitor_goods）暂不支持：电霸商品详情页要按商品 ID 查，先不接
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

from app.adapters.market_tool import dianba_parse
from app.adapters.types import CompetitorGoods, KeywordStat, RankItem
from app.config import get_settings
from app.data.channels.web_reader import profiles
from app.data.channels.web_reader.browser import browser_lock
from app.data.channels.web_reader.limiter import get_limiter
from app.data.errors import LoginExpired

log = logging.getLogger(__name__)
PLATFORM = "dianba"
BASE = "https://pdd.dianba6.com"
# 页面地址 → 要取的那个数据请求（地址里包含这段）
HOT_WORDS = ("/hotWord/rank", "search/hotWordList?page=1&page_size=1000")
HOT_GOODS = ("/product/productSell", "industry/hotGoods/getHotGoodsData")


class DianbaWeb:
    channel = "web"
    platform = PLATFORM

    def __init__(self, lane: str = "main", opener: Callable[[str, str], dict[str, Any]] | None = None,
                 today: Callable[[], date] = date.today):
        self.lane, self.today = lane, today
        self._open = opener or self._open_page
        self._cache: dict[str, dict[str, Any]] = {}  # 同一天同一页只读一次（选品会对每个需求反复取）

    @property
    def max_age(self) -> int:
        return get_settings().platforms[PLATFORM].get("max_data_age_days", 14)

    def _open_page(self, path: str, api: str) -> dict[str, Any]:
        """打开页面，返回 {url, data}：data 是页面请求 api 得到的 JSON。"""
        from playwright.sync_api import sync_playwright

        if profiles.window_open(PLATFORM):
            raise LoginExpired("电霸的登录窗口还开着，请先关掉")
        with browser_lock(PLATFORM, self.lane), sync_playwright() as p:
            get_limiter(PLATFORM).acquire(self.lane)
            ctx = p.chromium.launch_persistent_context(
                str(profiles.profile_dir(PLATFORM)), executable_path=profiles.browser_path(), headless=True,
                viewport={"width": 1440, "height": 900})
            try:
                page = ctx.new_page()
                with page.expect_response(lambda r: api in r.url, timeout=45000) as resp:
                    page.goto(BASE + path, wait_until="domcontentloaded", timeout=45000)
                return {"url": page.url, "data": resp.value.json()}
            except Exception:
                if "/login" in page.url:
                    return {"url": page.url, "data": {}}
                raise
            finally:
                ctx.close()

    def _read(self, page: tuple[str, str]) -> dict[str, Any]:
        key = f"{self.today()}:{page[0]}"  # 按天缓存，第二天重新读
        if key not in self._cache:
            got = self._open(*page)
            if any(w in got["url"] for w in profiles.login_defs()[PLATFORM].logged_out):
                profiles.mark_logged_out(PLATFORM, got["url"])
                raise LoginExpired("电霸登录已失效，请在「接入状态」里重新登录")
            self._cache[key] = got["data"]
        return self._cache[key]

    def hot_keywords(self, category: str, week: str) -> list[KeywordStat]:
        words, day = dianba_parse.hot_words(self._read(HOT_WORDS), category)
        dianba_parse.check_fresh(day, self.today(), self.max_age, "热词")
        return words

    def category_ranking(self, category: str, week: str) -> list[RankItem]:
        goods, day = dianba_parse.hot_goods(self._read(HOT_GOODS), category)
        dianba_parse.check_fresh(day, self.today(), self.max_age, "热销产品")
        return goods

    def competitor_goods(self, goods_url: str) -> CompetitorGoods:
        raise LookupError("电霸暂不支持按链接查竞品，请直接给商品名和售价")
