"""Web 控制台其余页面的接口：闭环总览、选品中心、任务看板、数据中心、规则配置（含版本）、成本。"""
from datetime import timedelta

from fastapi.testclient import TestClient

from app.config import get_settings
from app.db import session_scope, utcnow
from app.models import FetchJob, RuleConfig
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill

H_OWNER, H_OP = {"X-Open-Id": OWNER}, {"X-Open-Id": OPERATOR}


def _client() -> TestClient:
    from app import main

    main.init = lambda **_: None
    return TestClient(main.app)


def _seed(services, gateway):
    backfill(services, DAY, 14)
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
    run_until_idle(services, gateway)


def test_overview_selection_board_costs(env):
    services, gateway = env
    _seed(services, gateway)
    with _client() as c:
        o = c.get("/api/overview").json()
        assert o["day"] == DAY.isoformat() and [x["step"] for x in o["loop"]][:4] == ["采集", "指标", "诊断", "早报"]
        assert o["report"]["best"] == "柑橘香薰蜡烛" and len(o["pending_suggestions"]) == 1
        f = o["funnel"]
        assert f["sourced"] == 9 and f["recommended"] == 1 and f["sourced"] >= f["filtered"] >= f["verified"] >= f["recommended"]

        d = c.get("/api/candidates").json()
        assert d["week"] == "2026-W43" and d["weeks"] == ["2026-W43"]
        top = d["candidates"][0]
        assert top["status"] == "recommended" and top["demand"]["seed_type"] == "extension"
        assert top["backups"][0]["title"].startswith("葡萄柚香薰蜡烛 玻璃杯")
        assert any(x["reject_reason"].startswith("不支持一件代发") for x in d["candidates"])
        assert c.get("/api/test-runs").json() == []

        # 店主在 Web 拍板 → 派活 → 任务进入「待写入确认」列
        sug = o["pending_suggestions"][0]
        assert c.post(f"/api/suggestion/{sug['id']}/actions/approve", headers=H_OP).status_code == 403
        assert c.post(f"/api/suggestion/{sug['id']}/actions/approve", headers=H_OWNER).json() == {"queued": True}
        run_until_idle(services, gateway)
        cols = {x["key"]: x for x in c.get("/api/board").json()["columns"]}
        card = cols["confirm"]["tasks"][0]
        assert card["executor"] == "image" and card["ref"].startswith(f"建议 #{sug['id']}") and card["output"]["patch"]

        costs = c.get("/api/costs?days=7").json()
        assert len(costs["days"]) == 7 and costs["days"][-1]["main"] > 0
        assert {r["key"] for r in costs["by_agent"]} >= {"ops.diagnose", "ceo.daily", "selection.compliance"}
        assert costs["batch_saved"] > 0 and costs["batches"][0]["status"] == "ended"
        assert costs["budget"]["daily"] == 8 and costs["budget"]["over"] == ""


def test_data_center_and_resume(env):
    services, gateway = env
    backfill(services, DAY, 3)
    with session_scope() as s:
        s.add(FetchJob(item="promotion_daily", day=DAY - timedelta(days=1), status="circuit_open", channel="web", error="访问受限"))
    with _client() as c:
        d = c.get("/api/data/fetch-jobs?days=7").json()
        assert d["days"][0] == (DAY - timedelta(days=1)).isoformat() and len(d["days"]) == 7  # 最新入库的一天
        assert {i["key"] for i in d["items"]} >= {"goods_snapshot", "order_stats"}
        job = next(j for j in d["jobs"] if j["status"] == "circuit_open")
        assert c.post(f"/api/fetch_job/{job['id']}/resume", headers=H_OP).status_code == 403  # 熔断只有店主能恢复
        assert "24 小时" in c.post(f"/api/fetch_job/{job['id']}/resume", headers=H_OWNER).json()["detail"]
        with session_scope() as s:
            s.get(FetchJob, job["id"]).updated_at = utcnow() - timedelta(hours=25)
        assert c.post(f"/api/fetch_job/{job['id']}/resume", headers=H_OWNER).json() == {"queued": True}
        ev = c.get("/api/data/events").json()
        assert set(ev) == {"risks", "changes", "imports"}


def test_rules_versions_and_validation(env):
    services, gateway = env
    with _client() as c:
        r = c.get("/api/rules").json()
        assert r["version"] == 0 and r["rules"]["selection"]["candidate_min_score"] == 70 and r["history"] == []
        patch = {"selection": {"candidate_min_score": 72}}
        assert c.put("/api/rules", json=patch, headers=H_OP).status_code == 403
        bad = c.put("/api/rules", json={"selection": {"score_weights": {"profit": 30}}}, headers=H_OWNER)
        assert bad.status_code == 400 and "合计应为 100" in bad.json()["detail"]
        assert "不认识" in c.put("/api/rules", json={"selection": {"nope": 1}}, headers=H_OWNER).json()["detail"]
        assert "数字" in c.put("/api/rules", json={"ops": {"review_window_days": "七"}}, headers=H_OWNER).json()["detail"]
        assert c.put("/api/rules", json=patch, headers=H_OWNER).json() == {"version": 1}
        assert get_settings().rules["selection"]["candidate_min_score"] == 72  # web 进程立即生效
        c.put("/api/rules", json={"selection": {"hard_filters": {"banned_categories": ["食品"]}}}, headers=H_OWNER)
        r = c.get("/api/rules").json()
        assert r["version"] == 2 and r["rules"]["selection"]["candidate_min_score"] == 72  # 新版本保留之前的改动
        assert r["history"][0]["changes"][0].startswith("selection.hard_filters.banned_categories")
        assert r["history"][1]["changes"] == ["selection.candidate_min_score: 70 → 72"]

    # worker 侧：其他进程改了规则，下一个作业开始前生效；删掉版本后回到 rules.yaml
    from app.workflow import rule_config

    with session_scope() as s:
        s.query(RuleConfig).filter_by(key="rules").delete()
        enqueue(s, "system.timeouts", {})
    run_until_idle(services, gateway)
    assert get_settings().rules["selection"]["candidate_min_score"] == 70
    assert rule_config._applied_id is None
