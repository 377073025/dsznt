from fastapi.testclient import TestClient

from app.db import session_scope
from app.models import Job, Shop, Suggestion
from tests.conftest import DAY, OPERATOR, OWNER


def _client(env) -> TestClient:
    from app import main

    main.init = lambda **_: None  # conftest 已初始化测试库
    return TestClient(main.app)


def test_health_and_capabilities(env):
    with _client(env) as c:
        h = c.get("/api/health").json()
        assert h["mode"] == "fake" and h["capabilities"]["pdd_api"] == "not_configured"
        apps = c.get("/api/applications").json()
        assert [a["id"] for a in apps][:2] == ["R01", "R02"] and len(apps) == 15


def test_credential_requires_owner_and_never_returned(env):
    with _client(env) as c:
        r = c.post("/api/capabilities/llm/credential", json={"ref": "anthropic_api_key", "value": "sk-test-123456"},
                   headers={"X-Open-Id": OPERATOR})
        assert r.status_code == 403
        r = c.post("/api/capabilities/pdd_api/credential", json={"ref": "pdd_client_secret", "value": "secret-xyz"},
                   headers={"X-Open-Id": OWNER})
        assert r.json()["status"] == "configured"
        assert "secret-xyz" not in c.get("/api/capabilities").text


def test_credential_form_saves_fields_together(env):
    with _client(env) as c:
        own = {"X-Open-Id": OWNER}
        cap = {x["key"]: x for x in c.get("/api/capabilities").json()}
        assert [f["ref"] for f in cap["feishu_bot"]["credentials"]["fields"]] == ["feishu_app_id", "feishu_app_secret"]
        assert cap["pdd_web"]["credentials"] is None  # 网页读取靠浏览器登录
        url = "/api/capabilities/feishu_bot/credentials"
        assert c.put(url, json={"values": {"feishu_app_id": "cli_x"}}, headers=own).json()["detail"] == "还缺：App Secret"
        assert c.put(url, json={"values": {"evil_ref": "x"}}, headers=own).status_code == 400
        r = c.put(url, json={"values": {"feishu_app_id": "cli_x", "feishu_app_secret": " s3cret-value "}}, headers=own)
        assert r.json()["status"] == "configured"
        # 再改时只填一项也行，另一项保留
        assert c.put(url, json={"values": {"feishu_app_secret": "new-value", "feishu_app_id": ""}}, headers=own).json()["saved"] == ["feishu_app_secret"]
        text = c.get("/api/capabilities").text
        assert "s3cret" not in text and "new-value" not in text
        assert all(f["set"] for f in {x["key"]: x for x in c.get("/api/capabilities").json()}["feishu_bot"]["credentials"]["fields"])
        assert c.put(url, json={"values": {"feishu_app_secret": "x"}}, headers={"X-Open-Id": OPERATOR}).status_code == 403


def test_web_console_approve_uses_same_rules(env):
    with session_scope() as s:
        sug = Suggestion(shop_id=s.query(Shop).first().id, day=DAY, type="main_image", action="a", reason="r", status="reported")
        s.add(sug)
        s.flush()
        sid = sug.id
    with _client(env) as c:
        assert c.post(f"/api/suggestion/{sid}/actions/approve", headers={"X-Open-Id": OPERATOR}).status_code == 403
        assert c.post(f"/api/suggestion/{sid}/actions/approve", headers={"X-Open-Id": OWNER}).json() == {"queued": True}
    with session_scope() as s:
        assert s.query(Job).filter_by(type="card.apply").count() == 1


def test_upload_import(env, tmp_path):
    body = "日期,商品ID,商品访客数,商品加购件数,支付订单数\n2026-10-23,g_haiyan,350,20,8\n".encode("utf-8-sig")
    with _client(env) as c:
        r = c.post("/api/imports", files={"file": ("x.csv", body, "text/csv")}, headers={"X-Open-Id": OPERATOR})
        assert r.json()["status"] == "imported"
