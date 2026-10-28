"""1688 网页读取（07 号文档 6.3 节）：用专用浏览器（已登录的采集账号）打开搜索页和详情页，只读。

- 每翻一页先过限速器（platforms.yaml 的 alibaba.rate_limit），同一时刻只有一个任务用这个浏览器
- 页面出现验证、风控提示或跳到登录页就立刻停下：记为未登录，抛出异常交给上层暂停并通知人工；不做任何绕过
- 以图搜要上传图片，暂不支持（返回空列表，上层退到关键词搜索）
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote

from app.adapters.alibaba1688 import web_parse
from app.adapters.types import OfferBrief, OfferDetail, OfferFilter, SupplierProfile
from app.config import get_settings
from app.data.channels.web_reader import profiles
from app.data.channels.web_reader.browser import browser_lock
from app.data.channels.web_reader.limiter import get_limiter
from app.data.errors import CaptchaRequired, LoginExpired, RiskBlocked

log = logging.getLogger(__name__)
PLATFORM = "alibaba"
SEARCH_URL = "https://s.1688.com/selloffer/offer_search.htm?keywords={kw}&charset=utf8"
DETAIL_URL = "https://detail.1688.com/offer/{id}.html"


def check_risk(url: str, text: str) -> None:
    kw = get_settings().platforms["risk_keywords"]
    head = text[:3000]  # 只看页面开头：商品详情里也可能出现「验证」之类的字
    if any(w in head for w in kw["captcha"]) or "punish" in url:
        raise CaptchaRequired(f"1688 出现验证：{url[:80]}")
    if any(w in head for w in kw["blocked"]):
        raise RiskBlocked(f"1688 出现风控提示：{head[:60]}")
    if any(w in url for w in profiles.login_defs()[PLATFORM].logged_out):
        raise LoginExpired("1688 跳到了登录页")


class Alibaba1688Web:
    channel = "web"

    def __init__(self, lane: str = "main", opener: Callable[[str], dict[str, Any]] | None = None):
        self.lane = lane
        self._open = opener or self._open_page

    def _open_page(self, url: str) -> dict[str, Any]:
        """打开一页，返回 url / title / text / html / links。"""
        from playwright.sync_api import sync_playwright

        if profiles.window_open(PLATFORM):
            raise LoginExpired("1688 的登录窗口还开着，请先关掉")
        with browser_lock(PLATFORM, self.lane), sync_playwright() as p:
            get_limiter(PLATFORM).acquire(self.lane)
            ctx = p.chromium.launch_persistent_context(
                str(profiles.profile_dir(PLATFORM)), executable_path=profiles.browser_path(), headless=True,
                viewport={"width": 1440, "height": 900})
            try:
                page = ctx.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(6000)
                return {"url": page.url, "title": page.title(), "text": page.inner_text("body"), "html": page.content(),
                        "links": page.eval_on_selector_all(
                            "a[href]", "els => els.map(e => [e.innerText, e.href])")}
            finally:
                ctx.close()

    def _read(self, url: str) -> dict[str, Any]:
        page = self._open(url)
        try:
            check_risk(page["url"], page["text"])
        except (CaptchaRequired, LoginExpired, RiskBlocked):
            profiles.mark_logged_out(PLATFORM, page["url"])
            raise
        return page

    def search_by_keyword(self, kw: str, filters: OfferFilter, limit: int) -> list[OfferBrief]:
        page = self._read(SEARCH_URL.format(kw=quote(kw)))
        briefs = web_parse.parse_search(page["links"], limit=limit * 2)
        if filters.max_price_fen is not None:
            briefs = [b for b in briefs if b.price_fen <= filters.max_price_fen]
        return briefs[:limit]  # 是否代发要看详情页，上层按详情过滤

    def search_by_image(self, image: Path, limit: int) -> list[OfferBrief]:
        log.info("1688 网页读取暂不支持以图搜，跳过：%s", image)
        return []

    def offer_detail(self, offer_id: str) -> OfferDetail:
        num = offer_id.removeprefix("1688_")
        page = self._read(DETAIL_URL.format(id=num))
        return web_parse.parse_detail(f"1688_{num}", page["title"], page["text"], page["html"])

    def supplier_profile(self, member_id: str) -> SupplierProfile:
        raise LookupError("网页读取从详情页取供应商信息，没有单独的供应商档案")
