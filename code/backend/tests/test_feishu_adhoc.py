"""飞书里的单独任务：指令提交、权限、查看与取消、完成后私聊通知、卡片上「转为候选」。"""
from app.db import session_scope
from app.interfaces.feishu.handler import on_card_action
from app.models import AdhocTask, Candidate
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill


def _say(services, gateway, open_id, text, n=[0]):
    n[0] += 1
    with session_scope() as s:
        enqueue(s, "feishu.message", {"open_id": open_id, "chat_id": "oc_p2p", "chat_type": "p2p",
                                      "message_id": f"om_a{n[0]}", "msg_type": "text", "text": text})
    run_until_idle(services, gateway)
    return [m for m in services.feishu.sent if m.get("chat_id") == "oc_p2p"][-1]["text"]


def test_submit_notify_and_promote_from_feishu(env):
    services, gateway = env
    backfill(services, DAY, 1)
    reply = _say(services, gateway, OPERATOR, "评估 https://detail.1688.com/offer/6543210.html 29.9 葡萄柚香薰")
    assert reply.startswith("已提交单独任务 #1（单品评估）")
    with session_scope() as s:
        t = s.get(AdhocTask, 1)
        assert t.status == "done" and t.params["sell_price_fen"] == 2990 and t.note.startswith("飞书：")
    # 完成后私聊提交人；没有需求热度时评估分数不够，卡片不带转化按钮
    note = [m for m in services.feishu.sent if m.get("open_id") == OPERATOR][-1]
    assert "单独任务 #1 · 单品评估 · 已完成" in note["card"]["header"]["title"]["content"]
    assert "未通过" in str(note["card"]) and not any(e.get("tag") == "action" for e in note["card"]["elements"])

    assert "已提交单独任务 #2（打包上架物料）" in _say(services, gateway, OPERATOR, "物料 6543210 29.9 葡萄柚香薰")
    note = [m for m in services.feishu.sent if m.get("open_id") == OPERATOR][-1]
    assert "物料包" in str(note["card"])
    btn = next(a["value"] for e in note["card"]["elements"] if e.get("tag") == "action" for a in e["actions"])
    assert btn["entity"] == "adhoc_task" and btn["event"] == "promote"
    with session_scope() as s:
        assert "权限" in on_card_action(s, "ou_viewer", btn)["toast"]["content"]
        assert on_card_action(s, OPERATOR, btn)["toast"]["content"] == "已收到，处理中"
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert s.get(AdhocTask, 2).status == "promoted"
        assert s.query(Candidate).filter_by(status="recommended").count() == 1
        assert "已经处理过" in on_card_action(s, OPERATOR, btn)["toast"]["content"]
    assert any("已转化" in str(u["card"]) for u in services.feishu.updated)


def test_commands_list_cancel_and_errors(env):
    services, gateway = env
    backfill(services, DAY, 1)
    assert "店主和运营" in _say(services, gateway, "ou_viewer", "诊断 海盐")
    assert "找不到商品「不存在」" in _say(services, gateway, OPERATOR, "诊断 不存在")
    assert "格式：文案 商品 要求" in _say(services, gateway, OPERATOR, "文案 海盐")
    assert "不是售价" in _say(services, gateway, OPERATOR, "评估 6543210 便宜点")
    assert "对应多个商品" in _say(services, gateway, OPERATOR, "复盘 香薰")
    assert "已提交" in _say(services, gateway, OPERATOR, "/抠图 海盐")
    assert "已提交" in _say(services, gateway, OPERATOR, "诊断")
    listing = _say(services, gateway, OPERATOR, "我的任务")
    assert "#2 诊断 · 已完成" in listing and "#1 修图 · 已完成" in listing
    assert "不能取消" in _say(services, gateway, OPERATOR, "取消 2")
    assert "不存在" in _say(services, gateway, OWNER, "转化 99")
    help_text = _say(services, gateway, OWNER, "帮助")
    assert "单独任务" in help_text and "补货源" in help_text
    # 不是指令的话照常交给 CEO 对话
    assert "转化率" in _say(services, gateway, OWNER, "海盐最近怎么样")
