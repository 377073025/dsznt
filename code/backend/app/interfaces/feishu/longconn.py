"""飞书长连接（07 号文档 9.1 节）：本地部署不需要公网地址。

- 接收消息事件 → 入队 feishu.message 作业，立即返回
- 卡片回传交互 → on_card_action（3 秒内完成：校验、幂等、入队），返回 toast

注意：lark-oapi 的长连接客户端在导入时绑定事件循环，必须在主线程运行；
worker 在这种模式下把作业循环放到后台线程。
"""
from __future__ import annotations

import json
import logging
from typing import Any

import lark_oapi as lark
from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger, P2CardActionTriggerResponse

from app.db import session_scope
from app.interfaces.feishu.handler import on_card_action
from app.workflow.jobs import enqueue

log = logging.getLogger(__name__)


def message_payload(ev: P2ImMessageReceiveV1) -> dict[str, Any] | None:
    """把 SDK 事件转成 feishu.message 作业的参数；不支持的消息类型返回 None。"""
    msg, sender = ev.event.message, ev.event.sender
    content = json.loads(msg.content or "{}")
    base = {"open_id": sender.sender_id.open_id, "chat_id": msg.chat_id, "chat_type": msg.chat_type,
            "message_id": msg.message_id, "msg_type": msg.message_type}
    if msg.message_type == "text":
        return {**base, "text": content.get("text", "")}
    if msg.message_type == "file":
        return {**base, "file_key": content["file_key"], "file_name": content.get("file_name", "")}
    return None


def _on_message(ev: P2ImMessageReceiveV1) -> None:
    payload = message_payload(ev)
    if payload is None:
        return
    with session_scope() as s:
        enqueue(s, "feishu.message", payload, dedupe_key=f"msg:{payload['message_id']}")  # 飞书可能重推同一条消息


def _on_card(ev: P2CardActionTrigger) -> P2CardActionTriggerResponse:
    try:
        with session_scope() as s:
            result = on_card_action(s, ev.event.operator.open_id, ev.event.action.value or {})
    except Exception:  # noqa: BLE001  回调不能抛错，否则飞书端显示失败
        log.exception("card action failed")
        result = {"toast": {"type": "error", "content": "处理失败，请稍后再试"}}
    return P2CardActionTriggerResponse(result)


def build_handler() -> lark.EventDispatcherHandler:
    return (lark.EventDispatcherHandler.builder("", "")
            .register_p2_im_message_receive_v1(_on_message)
            .register_p2_card_action_trigger(_on_card)
            .build())


def run_forever(app_id: str, app_secret: str) -> None:
    """阻塞运行；SDK 自带断线重连。"""
    lark.ws.Client(app_id, app_secret, event_handler=build_handler(), log_level=lark.LogLevel.INFO).start()
