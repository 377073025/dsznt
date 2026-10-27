"""Web「用户与权限」「商品与素材」「接入状态」与飞书「设为运营 @某人」。"""
from app.db import session_scope
from app.models import Asset, AuditLog, Product, User
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill
from tests.test_console_api import H_OP, H_OWNER, _client
from tests.test_on_sale import _listed


def test_users_roles(env):
    with _client() as c:
        c.get("/api/me", headers={"X-Open-Id": "ou_new"})  # 第一次访问自动登记为查看
        d = c.get("/api/users", headers=H_OP).json()
        assert d["roles"]["operator"] == "运营"
        new = next(u for u in d["users"] if u["open_id"] == "ou_new")
        assert new["role"] == "viewer"
        assert c.patch(f"/api/users/{new['id']}", json={"role": "operator"}, headers=H_OP).status_code == 403
        r = c.patch(f"/api/users/{new['id']}", json={"role": "operator", "name": "小王"}, headers=H_OWNER).json()
        assert r == {"id": new["id"], "name": "小王", "role": "operator"}
        owner = next(u for u in d["users"] if u["open_id"] == OWNER)
        bad = c.patch(f"/api/users/{owner['id']}", json={"role": "viewer"}, headers=H_OWNER)
        assert bad.status_code == 400 and "至少要保留一位店主" in bad.json()["detail"]
        assert c.patch(f"/api/users/{new['id']}", json={"role": "boss"}, headers=H_OWNER).status_code == 400
    with session_scope() as s:
        assert s.query(AuditLog).filter_by(action="user.set_role").count() == 1


def _say(services, gateway, open_id, text, mentions):
    with session_scope() as s:
        enqueue(s, "feishu.message", {"open_id": open_id, "chat_id": "oc_g", "chat_type": "group", "message_id": text,
                                      "msg_type": "text", "text": text, "mentions": mentions})
    run_until_idle(services, gateway)
    return [m for m in services.feishu.sent if m.get("chat_id") == "oc_g"][-1]["text"]


def test_feishu_set_role_by_mention(env):
    services, gateway = env
    bot = {"key": "@_user_1", "open_id": "ou_bot", "name": "店铺助手"}
    zhang = {"key": "@_user_2", "open_id": "ou_zhang", "name": "张三"}
    assert "只有店主" in _say(services, gateway, OPERATOR, "@_user_1 设为运营 @_user_2", [bot, zhang])
    assert "请在指令后 @" in _say(services, gateway, OWNER, "@_user_1 设为运营", [bot])
    assert _say(services, gateway, OWNER, "@_user_1 设为运营 @_user_2", [bot, zhang]) == "已把 张三 设为运营。"
    with session_scope() as s:
        assert s.query(User).filter_by(feishu_open_id="ou_zhang").one().role == "operator"
        assert s.query(User).filter_by(feishu_open_id="ou_bot").one_or_none() is None  # @ 机器人的不算
    me = {"key": "@_user_2", "open_id": OWNER, "name": "店主"}
    assert "至少要保留一位店主" in _say(services, gateway, OWNER, "@_user_1 设为查看 @_user_2", [bot, me])


def test_catalog_assets_and_source(env):
    services, gateway = env
    _listed(services, gateway)
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    with _client() as c:
        rows = {p["goods_id"]: p for p in c.get("/api/catalog", headers=H_OP).json()}
        new = rows["g_new_1"]
        assert new["source"]["offer_id"] == "1688_6543210" and new["source"]["price_fen"] == 1180
        assert new["trial"]["round"] == 1 and rows["g_citrus"]["source"] is None
        # 上架物料包用到的货源图登记在素材库，回填商品 ID 后跟着商品走
        assert [(a["kind"], a["origin"], a["version"]) for a in new["assets"]] == [("main", "supplier", 1)]

        assert c.put("/api/products/g_citrus/source", json={"offer": "x"}, headers={"X-Open-Id": "ou_v"}).status_code == 403
        assert c.put("/api/products/g_citrus/source", json={"offer": "abc"}, headers=H_OP).status_code == 400
        r = c.put("/api/products/g_citrus/source", json={"offer": "https://detail.1688.com/offer/6543299.html"}, headers=H_OP)
        assert r.json() == {"goods_id": "g_citrus", "source_offer_id": "1688_6543299"}
        assert c.put("/api/products/nope/source", json={"offer": ""}, headers=H_OP).status_code == 404
    with session_scope() as s:
        assert s.query(Product).filter_by(goods_id="g_citrus").one().source_offer_id == "1688_6543299"


def test_image_task_records_asset_versions(env):
    from app.agents.executors import record_asset

    with session_scope() as s:
        record_asset(s, "g_haiyan", "main", "a.jpg", "ai")
        b = record_asset(s, "g_haiyan", "main", "b.jpg", "ai", "换成卧室场景")
        assert b.asset_version == 2 and record_asset(s, "g_haiyan", "scene", "c.jpg", "ai").asset_version == 1
        assert s.query(Asset).count() == 3


def test_integrations_permissions(env):
    with _client() as c:
        apps = c.get("/api/applications").json()
        a = apps[0]["id"]
        assert c.patch(f"/api/applications/{a}", json={"status": "doing"}, headers={"X-Open-Id": "ou_v"}).status_code == 403
        assert c.patch(f"/api/applications/{a}", json={"status": "doing", "note": "已提交"}, headers=H_OP).json()["status"] == "doing"
