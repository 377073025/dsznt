"""专用浏览器登录入口：开放平台标记为不使用，网页读取能力挂上登录入口。"""
from fastapi.testclient import TestClient

from app.data.channels.web_reader import profiles
from tests.conftest import OPERATOR, OWNER


def _client() -> TestClient:
    from app import main

    main.init = lambda **_: None
    return TestClient(main.app)


def test_capabilities_mark_unused_and_browser(env, monkeypatch, tmp_path):
    monkeypatch.setattr(profiles, "_state_file", lambda p: tmp_path / f"{p}.login.json")
    with _client() as c:
        cap = {x["key"]: x for x in c.get("/api/capabilities").json()}
        assert cap["pdd_api"]["unused"] and cap["alibaba_api"]["unused"] and not cap["llm"]["unused"]
        assert cap["pdd_web"]["browser"] == "pdd_backend" and cap["alibaba_web"]["browser"] == "alibaba"
        bs = {b["platform"]: b for b in c.get("/api/browsers").json()}
        assert bs["pdd_backend"]["logged_in"] is None and bs["pdd_backend"]["window_open"] is False


def test_open_needs_role_and_reports_errors(env, monkeypatch):
    monkeypatch.setitem(profiles._BROWSERS, "edge", ("Edge", []))
    monkeypatch.setenv("DSZNT_BROWSER", "")
    with _client() as c:
        assert c.post("/api/browsers/pdd_backend/open", headers={"X-Open-Id": "ou_nobody"}).status_code == 403
        r = c.post("/api/browsers/pdd_backend/open", headers={"X-Open-Id": OPERATOR})
        assert r.status_code == 409 and "Edge" in r.json()["detail"]
        assert c.post("/api/browsers/nope/check", headers={"X-Open-Id": OWNER}).status_code == 409


def test_login_state_judged_by_redirect(env, monkeypatch, tmp_path):
    """检查登录：落到登录页即未登录；结果写进状态文件。"""
    import sys
    import types

    monkeypatch.setattr(profiles, "profile_dir", lambda p: tmp_path / p)
    monkeypatch.setattr(profiles, "_state_file", lambda p: tmp_path / f"{p}.login.json")
    monkeypatch.setattr(profiles, "browser_path", lambda: "msedge.exe")
    landed = {"url": ""}

    class Page:
        def goto(self, *a, **k): pass
        def wait_for_timeout(self, ms): pass
        @property
        def url(self): return landed["url"]

    class Ctx:
        def new_page(self): return Page()
        def close(self): pass

    class PW:
        chromium = types.SimpleNamespace(launch_persistent_context=lambda *a, **k: Ctx())
        def __enter__(self): return self
        def __exit__(self, *a): return False

    fake = types.ModuleType("playwright.sync_api")
    fake.sync_playwright, fake.Error = PW, RuntimeError
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)

    landed["url"] = "https://mms.pinduoduo.com/login/?redirectUrl=x"
    assert profiles.check_login("pdd_backend")["logged_in"] is False
    landed["url"] = "https://mms.pinduoduo.com/home/"
    assert profiles.check_login("pdd_backend")["logged_in"] is True
    assert profiles.login_state("pdd_backend")["logged_in"] is True
