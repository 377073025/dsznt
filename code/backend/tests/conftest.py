from __future__ import annotations

import os
from datetime import date, timedelta

import pytest

os.environ["DSZNT_SECRETS"] = "memory"
os.environ["DSZNT_ADAPTER_MODE"] = "fake"

from app import bootstrap  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.llm.gateway import Gateway  # noqa: E402
from app.services import build_services, set_services  # noqa: E402

OWNER = "ou_owner"
OPERATOR = "ou_operator"
DAY = date(2026, 10, 23)  # 数据日期；海盐在 10-17 换了白底图


@pytest.fixture
def env(tmp_path):
    bootstrap.init(f"sqlite:///{(tmp_path / 'test.db').as_posix()}")
    bootstrap.ensure_shop("示例香薰店", "shop_demo")
    bootstrap.set_owner(OWNER, "店主")
    with session_scope() as s:
        from app.models import User

        s.add(User(feishu_open_id=OPERATOR, name="运营", role="operator"))
    services = build_services()
    set_services(services)
    return services, Gateway(services.llm)


def backfill(services, end: date, days: int) -> None:
    """补采历史数据（不触发分析）。"""
    from app.data import router
    from app.models import Shop
    from app.workflow.pipelines import DAILY_ITEMS

    with session_scope() as s:
        shop = s.query(Shop).first()
        for i in range(days, 0, -1):
            for item in DAILY_ITEMS:
                router.fetch(s, services, shop, item, end - timedelta(days=i))
