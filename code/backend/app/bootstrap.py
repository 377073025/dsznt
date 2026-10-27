"""启动初始化：建表、写入能力开关与申请清单、注册作业处理器和事件订阅。web 与 worker 共用。"""
from __future__ import annotations

import logging

from app import capabilities
from app.config import get_settings
from app.db import create_all, init_engine, session_scope
from app.models import Shop, User


def init(db_url: str | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    init_engine(db_url)
    create_all()
    # 导入即注册：作业处理器与事件订阅
    import app.interfaces.feishu.handler  # noqa: F401
    import app.interfaces.feishu.messages  # noqa: F401
    import app.workflow.pipelines  # noqa: F401
    import app.workflow.adhoc  # noqa: F401
    import app.workflow.timeouts  # noqa: F401

    with session_scope() as s:
        capabilities.seed(s, fake_mode=get_settings().adapter_mode == "fake")


def ensure_shop(name: str, platform_shop_id: str = "") -> int:
    with session_scope() as s:
        shop = s.query(Shop).first()
        if shop is None:
            shop = Shop(name=name, platform_shop_id=platform_shop_id)
            s.add(shop)
            s.flush()
        return shop.id


def set_owner(open_id: str, name: str = "") -> None:
    """第一个店主通过命令行指定（07 号文档 9.4 节）。"""
    with session_scope() as s:
        user = s.query(User).filter_by(feishu_open_id=open_id).first()
        if user is None:
            user = User(feishu_open_id=open_id, name=name)
            s.add(user)
        user.role = "owner"
