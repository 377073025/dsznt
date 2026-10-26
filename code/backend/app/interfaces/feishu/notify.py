"""通知服务：发卡片并记录卡片与实体的对应关系。飞书不可用时只记日志，由 Web 首页展示。"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import capabilities as caps
from app.config import get_settings
from app.models import CardMessage, RuleConfig
from app.services import Services

log = logging.getLogger(__name__)


CHAT_KEY = "feishu.report_chat_id"


def chat_id(s: Session) -> str:
    """推送群：配置文件里写了就用配置；否则用第一次在群里和机器人说话时自动记录的群。"""
    configured = get_settings().raw.get("feishu", {}).get("report_chat_id")
    if configured:
        return configured
    row = s.scalar(select(RuleConfig).where(RuleConfig.key == CHAT_KEY).order_by(RuleConfig.id.desc()))
    return row.value if row else "default_chat"


def remember_chat(s: Session, chat: str) -> bool:
    if s.scalar(select(RuleConfig.id).where(RuleConfig.key == CHAT_KEY)) is not None:
        return False
    s.add(RuleConfig(key=CHAT_KEY, value=chat, approved_by="system"))
    return True


def send_card(
    s: Session, services: Services, card: dict[str, Any], entity: str, entity_id: int,
    also: list[tuple[str, int]] | None = None,
) -> str | None:
    """also：同一张卡片里还带按钮的其他实体（例如早报里的每条建议）。"""
    if services.feishu is None or not caps.is_healthy(s, "feishu_bot"):
        log.warning("飞书不可用，卡片未发送：%s #%s", entity, entity_id)
        return None
    mid = services.feishu.send_card(chat_id(s), card)
    for e, i in [(entity, entity_id), *(also or [])]:
        s.add(CardMessage(message_id=mid, entity=e, entity_id=i, card=card))
    s.flush()
    return mid


def cards_for(s: Session, entity: str, entity_id: int) -> list[CardMessage]:
    return list(s.scalars(select(CardMessage).where(CardMessage.entity == entity, CardMessage.entity_id == entity_id)))
