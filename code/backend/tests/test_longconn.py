"""飞书长连接的事件转换（不连真实飞书）：用 SDK 自己的模型构造事件。"""
import json

from lark_oapi.api.im.v1 import P2ImMessageReceiveV1
from lark_oapi.event.callback.model.p2_card_action_trigger import P2CardActionTrigger

from app.db import session_scope
from app.interfaces.feishu import longconn
from app.models import Job, Shop, Suggestion
from tests.conftest import DAY, OWNER


def _msg(msg_type, content):
    return P2ImMessageReceiveV1({"event": {
        "sender": {"sender_id": {"open_id": OWNER}},
        "message": {"message_id": "om_1", "chat_id": "oc_1", "chat_type": "group",
                    "message_type": msg_type, "content": json.dumps(content)},
    }})


def test_message_payload():
    assert longconn.message_payload(_msg("text", {"text": "早报"}))["text"] == "早报"
    p = longconn.message_payload(_msg("file", {"file_key": "fk", "file_name": "a.xlsx"}))
    assert (p["msg_type"], p["file_key"], p["chat_type"]) == ("file", "fk", "group")
    assert longconn.message_payload(_msg("image", {"image_key": "x"})) is None


def test_message_enqueued_once(env):
    longconn._on_message(_msg("text", {"text": "早报"}))
    longconn._on_message(_msg("text", {"text": "早报"}))  # 飞书重推
    with session_scope() as s:
        assert s.query(Job).filter_by(type="feishu.message").count() == 1


def test_card_callback_returns_toast(env):
    with session_scope() as s:
        sug = Suggestion(shop_id=s.query(Shop).first().id, day=DAY, type="other", action="a", reason="r", status="reported")
        s.add(sug)
        s.flush()
        value = {"entity": "suggestion", "id": sug.id, "event": "approve", "version": sug.version}
    ev = P2CardActionTrigger({"event": {"operator": {"open_id": OWNER}, "action": {"value": value}}})
    resp = longconn._on_card(ev)
    assert resp.toast.content == "已收到，处理中"
    bad = P2CardActionTrigger({"event": {"operator": {"open_id": OWNER}, "action": {"value": {"entity": "x"}}}})
    assert longconn._on_card(bad).toast.type == "error"  # 回调内部出错也返回 toast，不抛异常
