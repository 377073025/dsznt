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
    return row.value if row else ""


def remember_chat(s: Session, chat: str) -> bool:
    if s.scalar(select(RuleConfig.id).where(RuleConfig.key == CHAT_KEY)) is not None:
        return False
    s.add(RuleConfig(key=CHAT_KEY, value=chat, approved_by="system"))
    return True


def send_card(
    s: Session, services: Services, card: dict[str, Any], entity: str, entity_id: int,
    also: list[tuple[str, int]] | None = None, to_user: str = "", chat: str = "",
) -> str | None:
    """also：同一张卡片里还带按钮的其他实体（例如早报里的每条建议）。to_user：私聊发给这个 open_id，不发群。
    chat：发到这个会话（例如店主正在对话的私聊）。都没给且还没有推送群时，私聊发给店主。"""
    if services.feishu is None or not caps.is_healthy(s, "feishu_bot"):
        log.warning("飞书不可用，卡片未发送：%s #%s", entity, entity_id)
        return None
    if to_user:
        mid = services.feishu.send_card_to_user(to_user, card)
    elif chat or chat_id(s):
        mid = services.feishu.send_card(chat or chat_id(s), card)
    else:
        mid = _send_to_owners(s, services, card)
        if mid is None:
            log.warning("没有推送群，也没能私聊到店主，卡片未发送：%s #%s", entity, entity_id)
            return None
    for e, i in [(entity, entity_id), *(also or [])]:
        s.add(CardMessage(message_id=mid, entity=e, entity_id=i, card=card))
    s.flush()
    return mid


def _send_to_owners(s: Session, services: Services, card: dict[str, Any]) -> str | None:
    """还没有推送群：私聊发给每个店主，返回第一条成功的消息 ID（占位账号之类发不出去的跳过）。"""
    from app.models import User

    first = None
    for u in s.scalars(select(User).where(User.role == "owner")):
        try:
            mid = services.feishu.send_card_to_user(u.feishu_open_id, card)
            first = first or mid
        except Exception as e:  # noqa: BLE001
            log.warning("私聊店主 %s 失败：%s", u.feishu_open_id, e)
    return first


def cards_for(s: Session, entity: str, entity_id: int) -> list[CardMessage]:
    return list(s.scalars(select(CardMessage).where(CardMessage.entity == entity, CardMessage.entity_id == entity_id)))
