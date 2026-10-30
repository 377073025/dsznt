"""代发助手王读取（R09 铺货代发工具）：它是 1688 服务市场里的应用，用 1688 专用浏览器（同一个 1688 账号）打开，
取页面自己请求回来的数据，只读。

- 每天最多读一次（首页 → 代销货品 → 关联货源三个页面），过 platforms.yaml 的 daifa.rate_limit；和 1688 读取共用浏览器锁
- 跳到登录页就记 1688 为未登录；页面出现验证就停下，不做任何绕过
- 自动采购开关、免密支付、店铺授权都由店主在代发助手王里设置，这里不碰
- 铺货（push_listing）暂未接：返回失败，上层提示店主在「链接铺货」里手动铺
"""
from __future__ import annotations

import logging
from datetime import date
from typing import Any, Callable

from app.adapters.alibaba1688.web import check_risk
from app.adapters.dropship_tool import daifa_parse as P
from app.adapters.types import AutoOrderStatus, DropshipOrder, ListingPackage, WriteResult
from app.data.channels.web_reader import profiles
from app.data.channels.web_reader.browser import browser_lock
from app.data.channels.web_reader.limiter import get_limiter
from app.data.errors import LoginExpired

log = logging.getLogger(__name__)
PLATFORM = "alibaba"  # 登录和浏览器用 1688 的
LIMIT = "daifa"
APP_URL = "https://ufuwu.1688.com/page/fuwu_work_isv_container.htm?role=buyer&appkey=6541416"
# 接口名 → 结果键；订单计数只要带拼多多店铺 ID 的那次
APIS = {"/m1688/get_service_end": "service", "/Distributeproxy/getProxyShopInfo": "shops",
        "/Orderreturn/getOrderMessages": "orders", "/Orderreturn/getItemList": "linked", "/ItemSync/getItemList": "items"}


class DaifaWeb:
    channel = "web"
    platform = PLATFORM

    def __init__(self, lane: str = "main", opener: Callable[[], dict[str, Any]] | None = None,
                 today: Callable[[], date] = date.today):
        self.lane, self.today = lane, today
        self._open = opener or self._open_pages
        self._cache: dict[str, dict[str, Any]] = {}

    def _open_pages(self) -> dict[str, Any]:
        """打开首页、代销货品、关联货源，返回 {url, service, shops, orders: [(请求, 返回)], linked, items}。"""
        import re
        import urllib.parse

        from playwright.sync_api import sync_playwright

        if profiles.window_open(PLATFORM):
            raise LoginExpired("1688 的登录窗口还开着，请先关掉")
        got: dict[str, Any] = {"orders": []}

        def on_response(r) -> None:
            m = re.search(r"_aop_target=([^&]+)", r.url)
            if not m:
                return
            api = urllib.parse.unquote(m.group(1)).split("?")[0].replace("https://zk1688.aiyongtech.com", "")
            key = APIS.get(api)
            if key is None:
                return
            try:
                if key == "orders":
                    got["orders"].append((r.request.post_data or "", r.json()))
                else:
                    got.setdefault(key, r.json())
            except Exception:  # noqa: BLE001  个别返回不是 JSON 就跳过
                log.warning("代发助手王返回解析失败：%s", api)

        with browser_lock(PLATFORM, self.lane), sync_playwright() as p:
            get_limiter(LIMIT).acquire(self.lane)
            ctx = p.chromium.launch_persistent_context(
                str(profiles.profile_dir(PLATFORM)), executable_path=profiles.browser_path(), headless=True,
                viewport={"width": 1600, "height": 1000})
            try:
                page = ctx.new_page()
                page.on("response", on_response)
                page.goto(APP_URL, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(14000)
                if any(w in page.url for w in profiles.login_defs()[PLATFORM].logged_out):
                    return {"url": page.url}
                check_risk(page.url, page.inner_text("body"))
                for _ in range(3):  # 关掉续费、授权提醒等弹窗
                    page.keyboard.press("Escape")
                    page.wait_for_timeout(600)
                frame = next(f for f in page.frames if "isv-container" in f.url)
                for menu in ("代销货品", "关联货源"):  # 只点左侧菜单，不点任何按钮
                    frame.get_by_text(menu, exact=True).first.click(timeout=8000)
                    page.wait_for_timeout(6000)
                    page.keyboard.press("Escape")
                return {"url": page.url, **got}
            finally:
                ctx.close()

    def status(self) -> dict[str, Any]:
        """当天的代发状态：店铺授权、软件到期、订单计数、已关联货源、未关联的在售商品。"""
        key = str(self.today())
        if key not in self._cache:
            got = self._open()
            if any(w in got["url"] for w in profiles.login_defs()[PLATFORM].logged_out):
                profiles.mark_logged_out(PLATFORM, got["url"])
                raise LoginExpired("1688 登录已失效（代发助手王用的也是它），请在「接入状态」里重新登录")
            missing = [k for k in ("service", "shops", "linked", "items") if k not in got]
            if missing:
                raise P.DaifaDataError(f"代发助手王页面没有读到 {missing}（页面可能改版）")
            shop = P.pdd_shop(got["shops"])
            orders = next((P.order_counts(b) for req, b in got["orders"] if f"shopId={shop['id']}" in req), None)
            self._cache[key] = {"shop": shop, "service_end": P.service_end(got["service"]), "orders": orders,
                                "linked": P.linked_goods(got["linked"]), "unlinked": P.unlinked_onsale(got["items"])}
        return self._cache[key]

    def check_auto_order(self, offer_id: str) -> AutoOrderStatus:
        """货源已在代发助手王里关联过 → 能自动采购；没关联过的新货源不知道（铺货时才关联）。"""
        try:
            linked = {g["offer_id"] for g in self.status()["linked"]}
        except Exception as e:  # noqa: BLE001  读不到不影响选品打分，按「不知道」算
            log.warning("代发助手王读取失败，自动采购按未知处理：%s", e)
            return AutoOrderStatus(offer_id=offer_id, supported=None)
        return AutoOrderStatus(offer_id=offer_id, supported=True if offer_id in linked else None)

    def push_listing(self, pkg: ListingPackage) -> WriteResult:
        return WriteResult(ok=False, message="自动铺货还没接，请在代发助手王「铺货 → 链接铺货」粘贴货源链接："
                                             + pkg.source_offer_url)

    def order_sync_status(self, day: date) -> list[DropshipOrder]:
        """订单明细暂不读（只读计数，见 status()）。"""
        return []
