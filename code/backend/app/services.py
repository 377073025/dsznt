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
        for key, impl in (("llm", self.llm), ("feishu_bot", self.feishu)):
            if impl is not None and hasattr(impl, "health_check"):
                checks[key] = impl.health_check
        if self.mode == "real" and self.sourcing.get("web") is not None:
            from app.data.channels.web_reader import profiles

            checks["alibaba_web"] = lambda: profiles.require_logged_in("alibaba")
        return checks


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
    # 1688 走专用浏览器读网页；拼多多后台数据以人工导出为主（自动读取会触发平台验证）
    from app.adapters.alibaba1688.web import Alibaba1688Web

    return Services(mode="real", llm=llm, feishu=feishu, sourcing={"web": Alibaba1688Web()})


_services: Services | None = None


def get_services() -> Services:
    global _services
    if _services is None:
        _services = build_services()
    return _services


def set_services(s: Services) -> None:
    global _services
    _services = s
