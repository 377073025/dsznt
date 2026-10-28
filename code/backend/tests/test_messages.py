"""飞书消息：文件导入、固定指令、改一改、对话转指令、推送群自动记录。"""
from app.db import session_scope
from app.interfaces.feishu.handler import on_card_action
from app.interfaces.feishu.notify import chat_id
from app.models import ManualImport, Shop, Suggestion
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill


def _say(services, gateway, open_id, text, chat="oc_group", chat_type="group", n=[0]):
    n[0] += 1
    with session_scope() as s:
        enqueue(s, "feishu.message", {"open_id": open_id, "chat_id": chat, "chat_type": chat_type,
                                      "message_id": f"om_{n[0]}", "msg_type": "text", "text": text})
    run_until_idle(services, gateway)
    return services.feishu.sent[-1]


def test_group_remembered_and_commands(env):
    services, gateway = env
    _say(services, gateway, OWNER, "@_user_1 帮助")
    texts = [m.get("text", "") for m in services.feishu.sent]
    assert any("推送群" in t for t in texts) and "早报" in texts[-1]
    with session_scope() as s:
        assert chat_id(s) == "oc_group"
    status = _say(services, gateway, OWNER, "状态")["text"]
    assert "pdd_api 未接入" in status and "待拍板的建议：0 条" in status


def test_file_import_and_permission(env, tmp_path):
    services, gateway = env
    f = tmp_path / "商品数据.csv"
    f.write_text("日期,商品ID,商品访客数,商品加购件数,支付订单数\n2026-10-23,g_haiyan,350,20,8\n", encoding="utf-8-sig")
    services.feishu.files["fk_1"] = f
    for who, expect in ((OPERATOR, "已导入 1 行"), ("ou_stranger", "没有上传数据的权限")):
        with session_scope() as s:
            enqueue(s, "feishu.message", {"open_id": who, "chat_id": "oc_p2p", "chat_type": "p2p", "message_id": f"om_f_{who}",
                                          "msg_type": "file", "file_key": "fk_1"})
        run_until_idle(services, gateway)
        assert expect in services.feishu.sent[-1]["text"]
    with session_scope() as s:
        assert s.query(ManualImport).filter_by(status="imported").count() == 1


def test_revise_loop(env):
    services, gateway = env
    backfill(services, DAY + __import__("datetime").timedelta(days=1), 14)
    with session_scope() as s:
        sug = Suggestion(shop_id=s.query(Shop).first().id, day=DAY, goods_id="g_haiyan", type="main_image",
                         action="建议把海盐香薰蜡烛的主图换回原来的场景图", reason="因为转化率下降", evidence=[],
                         status="reported", needs_write=True)
        s.add(sug)
        s.flush()
        sid, version = sug.id, sug.version
        on_card_action(s, OWNER, {"entity": "suggestion", "id": sid, "event": "revise", "version": version})
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert s.get(Suggestion, sid).status == "revising"

    card = _say(services, gateway, OWNER, "先换主图的第二张，别动第一张")["card"]
    assert "修改后的建议（第 1 轮）" in card["header"]["title"]["content"]
    with session_scope() as s:
        sug = s.get(Suggestion, sid)
        assert sug.status == "reported" and sug.revise_rounds == 1 and "先换主图的第二张" in sug.action


def test_chat_answer_and_instruction(env):
    services, gateway = env
    backfill(services, DAY + __import__("datetime").timedelta(days=1), 14)
    reply = _say(services, gateway, OPERATOR, "海盐最近怎么样")["text"]
    assert reply.startswith("海盐香薰蜡烛近7天转化率")
    # 运营下达的指令不生成待拍板卡片；店主的才生成
    _say(services, gateway, OPERATOR, "帮我把雪松降价")
    with session_scope() as s:
        assert s.query(Suggestion).count() == 0
    card = _say(services, gateway, OWNER, "帮我把雪松降价")["card"]
    assert "请确认这条指令" in card["header"]["title"]["content"]
    with session_scope() as s:
        assert s.query(Suggestion).filter_by(status="reported", action="帮我把雪松降价").count() == 1


def test_card_without_push_group_goes_to_owner(env):
    """还没有推送群：卡片私聊发给店主；指定了会话就发到那个会话。"""
    from app.interfaces.feishu.notify import send_card

    services, _ = env
    with session_scope() as s:
        assert chat_id(s) == ""
        send_card(s, services, {"t": 1}, "report", 1)
        assert services.feishu.sent[-1]["open_id"] == OWNER
        send_card(s, services, {"t": 2}, "suggestion", 1, chat="oc_p2p")
        assert services.feishu.sent[-1]["chat_id"] == "oc_p2p"
