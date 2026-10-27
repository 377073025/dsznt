"""智能体工作台（07 号文档 17 节）：运行记录、单独任务、四道隔离、转为建议 / 候选。"""
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from app.adapters.llm.fake import FakeLLM
from app.adapters.llm.fake_responders import ops_diagnose
from app.agents.tracking import request_cancel
from app.data.channels.web_reader.limiter import DailyLimitReached, RateLimiter
from app.db import runs_scope, session_scope
from app.llm.gateway import Gateway
from app.models import (AdhocTask, AgentRun, AgentRunStep, AuditLog, Candidate, Job, LlmUsage, Report, Suggestion,
                        Task, User)
from app.tools.registry import ToolContext, ToolDenied, tools_for
from app.workflow import adhoc
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill


def _user(s, open_id):
    return s.query(User).filter_by(feishu_open_id=open_id).one()


def _submit(capability, params, who=OPERATOR):
    with session_scope() as s:
        return adhoc.submit(s, _user(s, who), capability, params).id


def _task(tid) -> AdhocTask:
    with session_scope() as s:
        return s.get(AdhocTask, tid)


def test_main_flow_writes_run_records(env):
    services, gateway = env
    backfill(services, DAY, 14)
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    with runs_scope() as rs:
        runs = {r.capability: r for r in rs.query(AgentRun)}
        assert runs["ops.diagnose"].status == "succeeded" and runs["ops.diagnose"].source == "main"
        assert runs["ops.diagnose"].label == "主流程 · daily.analyze" and runs["ops.diagnose"].turns == 1
        assert runs["ceo.daily"].model == "claude-opus-5-5" and runs["ceo.daily"].tokens > 0


def test_adhoc_diagnose_isolated_then_promoted(env):
    services, gateway = env
    backfill(services, DAY + timedelta(days=1), 14)
    tid = _submit("ops.diagnose", {"goods_id": "g_haiyan"})
    run_until_idle(services, gateway)
    t = _task(tid)
    assert t.status == "done" and t.result["suggestions"][0]["goods_id"] == "g_haiyan"
    with session_scope() as s:
        assert s.query(Suggestion).count() == 0 and s.query(Report).count() == 0  # 不进早报、不建建议
        assert {u.lane for u in s.query(LlmUsage)} == {"adhoc"}
    with runs_scope() as rs:
        run = rs.get(AgentRun, t.run_id)
        assert run.source == "adhoc" and run.label.startswith(f"单独任务 #{tid}")

    with session_scope() as s:
        ref = adhoc.promote(s, services, _user(s, OPERATOR), tid)
        sug = s.query(Suggestion).one()
        assert ref == f"suggestion:{sug.id}" and sug.status == "reported" and f"单独任务 #{tid}" in sug.reason
        assert s.get(AdhocTask, tid).status == "promoted"
    assert "来自单独任务" in services.feishu.sent[-1]["card"]["header"]["title"]["content"]


def test_adhoc_cannot_use_write_tools(env):
    services, _ = env
    with session_scope() as s:
        ctx = ToolContext(s, services, "ceo.dispatch", lane="adhoc")
        with pytest.raises(ToolDenied):
            tools_for("ceo.dispatch", ["create_task"])[0].invoke(ctx, {})
        assert s.query(AuditLog).filter_by(action="tool.denied").count() == 1


def test_isolation_guard_blocks_business_writes(env, monkeypatch):
    services, gateway = env
    from app.agents import catalog

    def sneaky(ctx, p):
        ctx.session.add(Suggestion(shop_id=1, day=DAY, type="other", action="偷偷建议", reason="r"))
        ctx.session.flush()

    cap = catalog.CATALOG["ceo.ask"]
    monkeypatch.setitem(catalog.CATALOG, "ceo.ask", cap.__class__(**{**cap.__dict__, "run": sneaky}))
    tid = _submit("ceo.ask", {"question": "随便"})
    run_until_idle(services, gateway)
    t = _task(tid)
    assert t.status == "failed" and "不能写 suggestion" in t.error
    with session_scope() as s:
        assert s.query(Suggestion).count() == 0


def test_adhoc_budget_separate_from_main(env):
    services, gateway = env
    backfill(services, DAY, 14)
    with session_scope() as s:
        s.add(LlmUsage(agent="x", model="m", cost_cny=99.0, lane="adhoc"))  # 单独任务预算已用完
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    tid = _submit("ops.diagnose", {})
    run_until_idle(services, gateway)
    t = _task(tid)
    assert t.status == "deferred" and "单独任务" in t.error
    with session_scope() as s:
        assert s.query(Report).count() == 1  # 主流程不受影响
        assert s.query(Job).filter_by(type="adhoc.run", status="queued").one().run_after > datetime.utcnow()


def test_main_lane_never_waits_for_adhoc(env):
    services, gateway = env
    backfill(services, DAY, 14)
    tid = _submit("ops.diagnose", {})
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway, lanes=("main",))  # 单独通道完全不动
    with session_scope() as s:
        assert s.query(Report).count() == 1
    assert _task(tid).status == "queued"


def test_cancel_queued_and_running(env):
    services, gateway = env
    backfill(services, DAY, 14)
    tid = _submit("ops.diagnose", {})
    with session_scope() as s:
        assert adhoc.cancel(s, _user(s, OPERATOR), tid).status == "cancelled"
    run_until_idle(services, gateway)
    assert _task(tid).status == "cancelled"

    tid2 = _submit("ops.diagnose", {})
    with session_scope() as s:
        s.add(User(feishu_open_id="ou_other_op", role="operator"))
    with session_scope() as s, pytest.raises(PermissionError):
        adhoc.cancel(s, _user(s, "ou_other_op"), tid2)  # 不是提交人

    def cancel_midway(req):
        request_cancel(tid2)  # 模拟人工在模型调用期间点了取消
        return ops_diagnose(req)

    llm = FakeLLM({"ops.diagnose": cancel_midway})
    services.llm = llm
    run_until_idle(services, Gateway(llm))
    assert _task(tid2).status == "cancelled"
    with runs_scope() as rs:
        assert rs.query(AgentRun).filter_by(adhoc_task_id=tid2).one().status == "cancelled"


def test_web_capability_deferred_during_blackout(env, monkeypatch):
    services, gateway = env
    monkeypatch.setattr(adhoc, "_now_cn", lambda: datetime(2026, 10, 24, 7, 0))
    tid = _submit("selection.evaluate_offer", {"offer": "1688_6543210", "sell_price_fen": 2990})
    run_until_idle(services, gateway)
    t = _task(tid)
    assert t.status == "deferred" and "08:30" in t.error


def test_evaluate_offer_and_promote_to_candidate(env, monkeypatch):
    services, gateway = env
    backfill(services, DAY, 1)
    monkeypatch.setattr(adhoc, "_now_cn", lambda: datetime(2026, 10, 24, 10, 0))
    tid = _submit("selection.evaluate_offer", {"offer": "https://detail.1688.com/offer/6543210.html",
                                               "sell_price_fen": 2990, "keyword": "葡萄柚香薰蜡烛", "trend": 0.34})
    bad = _submit("selection.evaluate_offer", {"offer": "1688_7000001", "sell_price_fen": 2990})
    run_until_idle(services, gateway)
    t = _task(tid)
    assert t.status == "done" and t.result["passed"] and "建议上新" in t.result["reason"]
    assert "不支持一件代发" in _task(bad).result["reject_reasons"]
    with session_scope() as s:
        assert s.query(Candidate).count() == 0
        with pytest.raises(adhoc.AdhocError):
            adhoc.promote(s, services, _user(s, OWNER), bad)
        adhoc.promote(s, services, _user(s, OWNER), tid)
        cand = s.query(Candidate).one()
        assert cand.status == "recommended" and cand.offer_id == "1688_6543210"


def test_promoted_copy_goes_through_owner_gate(env):
    services, gateway = env
    backfill(services, DAY, 1)
    tid = _submit("exec.copy", {"goods_id": "g_citrus", "instruction": "突出送礼"})
    run_until_idle(services, gateway)
    with session_scope() as s:
        adhoc.promote(s, services, _user(s, OPERATOR), tid)
        sug = s.query(Suggestion).one()
        assert sug.type == "title" and sug.needs_write and sug.status == "reported"
        assert s.query(Task).count() == 0  # 还要店主拍板，不会直接派活
    assert services._pdd_writer.calls == []


def test_adhoc_page_quota():
    lim = RateLimiter("pdd_front", sleep=lambda _: None, clock=lambda: datetime(2026, 10, 24, 9, 0))
    for _ in range(4):  # 20 页 × 20%
        lim.acquire(lane="adhoc")
    with pytest.raises(DailyLimitReached):
        lim.acquire(lane="adhoc")
    lim.acquire()  # 主流程照常
    assert lim.used_today == 5


def test_workbench_api(env):
    services, gateway = env
    from app import main

    main.init = lambda: None
    with TestClient(main.app) as c:
        caps = {x["key"]: x for x in c.get("/api/agent-capabilities").json()}
        assert len(caps) == 10 and "goods_id" in caps["ops.diagnose"]["params_schema"]["properties"]
        r = c.post("/api/adhoc", json={"capability": "ceo.ask", "params": {}}, headers={"X-Open-Id": OPERATOR})
        assert r.status_code == 422  # 缺少 question
        assert c.post("/api/adhoc", json={"capability": "ceo.ask", "params": {"question": "海盐怎么样"}},
                      headers={"X-Open-Id": "ou_viewer"}).status_code == 403
        tid = c.post("/api/adhoc", json={"capability": "ceo.ask", "params": {"question": "海盐怎么样"}},
                     headers={"X-Open-Id": OPERATOR}).json()["id"]
        ceo = next(a for a in c.get("/api/agents").json() if a["agent"] == "ceo")
        assert ceo["state"] == "queued" and f"单独任务 #{tid}" in ceo["queued"][0]

        backfill(services, DAY, 14)
        run_until_idle(services, gateway)
        task = c.get("/api/adhoc").json()[0]
        assert task["status"] == "done" and "海盐" in task["result"]["reply"]
        detail = c.get(f"/api/runs/{task['run_id']}").json()
        assert detail["status"] == "succeeded" and detail["source"] == "adhoc"
        assert c.get("/api/agents/ceo/runs?source=adhoc").json()[0]["id"] == task["run_id"]
        assert c.post(f"/api/adhoc/{tid}/promote", headers={"X-Open-Id": OPERATOR}).status_code == 400  # 问答不能转化


def test_run_steps_record_tools(env):
    services, gateway = env
    from app.agents import ceo
    from app.agents.runtime import AgentRunner

    with session_scope() as s:
        sug = Suggestion(shop_id=1, day=DAY, goods_id="g_haiyan", type="main_image", action="a", reason="r",
                         status="approved", needs_write=True)
        s.add(sug)
        s.flush()
        ceo.dispatch(AgentRunner(s, services, gateway), s, sug.id)
    with runs_scope() as rs:
        run = rs.query(AgentRun).filter_by(capability="ceo.dispatch").one()
        steps = rs.query(AgentRunStep).filter_by(run_id=run.id).all()
        assert run.turns == 2 and [x.name for x in steps] == ["create_task"] and "task_id" in steps[0].summary
