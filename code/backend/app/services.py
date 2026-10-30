"""适配器装配：按 adapter_mode 和能力开关，为每个通道提供具体实现。

模拟模式下所有通道都用 Fake 实现；真实模式下尚未实现的适配器返回 None，
通道路由会把它当作不可用而降级。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.adapters.alibaba1688.fake import FakeSourcing
from app.adapters.dropship_tool.fake import FakeDropship
from app.adapters.feishu.fake import FakeFeishu
from app.adapters.image_gen.fake import FakeImageGen
from app.adapters.llm.fake import FakeLLM
from app.adapters.market_tool.fake import FakeMarketData
from app.adapters.pdd.fake import FakePddShop, FakePddWriter
from app.config import Settings, get_settings
from app.secrets import get_secret


@dataclass
class Services:
    mode: str
    llm: Any
    feishu: Any
    # 通道名 → 实现，例如 {"api": ..., "web": ...}
    pdd: dict[str, Any] = field(default_factory=dict)
    sourcing: dict[str, Any] = field(default_factory=dict)
    market: Any = None
    dropship: Any = None
    image_gen: Any = None
    # 写店铺的实现单独存放，只通过 tools/write 取用
    _pdd_writer: Any = None

    def health_checks(self) -> dict[str, Any]:
        checks = {}
        for key, impl in (("llm", self.llm), ("feishu_bot", self.feishu), ("image_gen", self.image_gen)):
            if impl is not None and hasattr(impl, "health_check"):
                checks[key] = impl.health_check
        if self.mode == "real" and self.sourcing.get("web") is not None:
            from app.data.channels.web_reader import profiles

            checks["alibaba_web"] = lambda: profiles.require_logged_in("alibaba")
        for key, impl in self.browser_caps().items():
            from app.data.channels.web_reader import profiles

            checks[key] = lambda p=impl.platform: profiles.require_logged_in(p)
        return checks

    def browser_caps(self) -> dict[str, Any]:
        """借用别的平台专用浏览器的能力 → 实现（例如机会商品用拼多多后台、代发助手王用 1688），登录状态就是能力状态。"""
        if self.mode != "real":
            return {}
        return {k: v for k, v in (("market_tool", self.market), ("dropship_tool", self.dropship))
                if getattr(v, "platform", None)}


def build_services(settings: Settings | None = None) -> Services:
    settings = settings or get_settings()
    if settings.adapter_mode == "fake":
        dropship = FakeDropship()
        pdd = FakePddShop(dropship)
        sourcing = FakeSourcing()
        return Services(
            mode="fake",
            llm=FakeLLM(),
            feishu=FakeFeishu(),
            pdd={"api": pdd, "web": pdd},
            sourcing={"api": sourcing, "web": sourcing},
            market=FakeMarketData(),
            dropship=dropship,
            image_gen=FakeImageGen(),
            _pdd_writer=FakePddWriter(),
        )

    llm = None
    conf = settings.llm
    key = get_secret(conf["key_ref"])
    if key:
        from app.adapters.llm.anthropic_client import AnthropicLLM

        llm = AnthropicLLM(api_key=key, health_model=settings.model_for("light")["model"], base_url=conf.get("base_url"),
                           structured_output=conf.get("structured_output", True), supports_batch=conf.get("batch", True))
    feishu = None
    app_id, app_secret = get_secret("feishu_app_id"), get_secret("feishu_app_secret")
    if app_id and app_secret:
        from app.adapters.feishu.lark_client import LarkFeishu

        feishu = LarkFeishu(app_id, app_secret, download_dir=settings.path("imports_dir"))
    image_gen = None
    gen_conf = settings.raw.get("image_gen") or {}
    gen_key = get_secret(gen_conf.get("key_ref", "image_gen_api_key"))
    if gen_key and gen_conf.get("provider") == "seedream":
        from app.adapters.image_gen.seedream import SeedreamImageGen

        assets = settings.path("assets_dir")
        image_gen = SeedreamImageGen(gen_key, gen_conf["model"], out_dir=assets / "generated", base_url=gen_conf["base_url"],
                                     size=gen_conf.get("size", "2K"), assets_dir=assets)
    # 1688、市场数据、代发助手王走专用浏览器读网页；拼多多后台经营数据以人工导出为主（自动读取会触发平台验证）
    # 市场数据（R10）：market.provider 选 pdd_chance（商家后台机会商品，免费）或 dianba（电霸，付费）
    from app.adapters.alibaba1688.web import Alibaba1688Web
    from app.adapters.dropship_tool.daifa_web import DaifaWeb

    if (settings.raw.get("market") or {}).get("provider", "pdd_chance") == "dianba":
        from app.adapters.market_tool.dianba_web import DianbaWeb as Market
    else:
        from app.adapters.market_tool.pdd_chance_web import PddChanceWeb as Market
    return Services(mode="real", llm=llm, feishu=feishu, sourcing={"web": Alibaba1688Web()}, market=Market(),
                    dropship=DaifaWeb(), image_gen=image_gen)


_services: Services | None = None


def get_services() -> Services:
    global _services
    if _services is None:
        _services = build_services()
    return _services


def set_services(s: Services) -> None:
    global _services
    _services = s
