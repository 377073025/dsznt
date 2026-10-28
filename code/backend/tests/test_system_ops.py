"""运维与成本：备份、JSON 日志与脱敏、加列迁移、预算告警、月预算、周度系统报告、Batch。"""
import json
import logging
import sqlite3
from datetime import date, datetime, timedelta

from app import bootstrap
from app.backup import run_backup
from app.db import session_scope, utcnow
from app.llm.gateway import BudgetExceeded, Gateway
from app.logs import JsonFormatter, log_context, redact
from app.models import FetchJob, Job, LlmBatch, LlmUsage, Report, Suggestion
from app.workflow.jobs import enqueue, run_until_idle
from app.workflow.system import check_budget_alerts, weekly_system_report
from tests.conftest import DAY, OWNER, backfill


def test_backup_snapshot_mirror_and_prune(env, tmp_path):
    assets = tmp_path / "assets"
    (assets / "g1").mkdir(parents=True)
    (assets / "g1" / "a.jpg").write_bytes(b"img")
    root = tmp_path / "backup"
    old = root / "db" / "20200101"
    old.mkdir(parents=True)
    out = run_backup(root=root, assets=assets)
    assert out["databases"] == ["test.db", "test.runs.db"] and out["assets_copied"] == 1 and out["pruned"] == ["20200101"]
    day_dir = next((root / "db").iterdir())
    with sqlite3.connect(day_dir / "test.db") as c:
        assert c.execute("select count(*) from user").fetchone()[0] == 2  # 快照可直接打开
    assert run_backup(root=root, assets=assets)["assets_copied"] == 0  # 素材没变不重复复制


def test_redaction_and_json_context():
    assert redact("access_token=abc123xyz&x=1") == "access_token=***&x=1"
    # 飞书长连接地址里的连接凭据
    assert redact("wss://x/ws/v2?fpid=1&access_key=fe92&ticket=80f6-e1e5") == "wss://x/ws/v2?fpid=1&access_key=***&ticket=***"
    assert redact('{"api_key": "sk-ant-123"}') == '{"api_key": "***"}'
    assert redact("联系 13812345678") == "联系 138****5678"
    assert redact("身份证 110101199001011234") == "身份证 110101********1234"
    rec = logging.LogRecord("t", logging.INFO, __file__, 1, "hello %s", ("x",), None)
    with log_context(job_id=7, agent="ops.diagnose"):
        from app.logs import _ContextFilter

        _ContextFilter().filter(rec)
    d = json.loads(JsonFormatter().format(rec))
    assert d["msg"] == "hello x" and d["job_id"] == 7 and d["agent"] == "ops.diagnose"


def test_missing_columns_added_to_old_db(tmp_path):
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as c:  # 旧版本的测款表：没有 days / trial_round
        c.execute("create table test_run (id integer primary key, candidate_id integer, goods_id varchar(64), "
                  "start_date date, visitors integer, cvr float, verdict varchar(16), created_at datetime, "
                  "updated_at datetime, version integer)")
        c.execute("insert into test_run (candidate_id, goods_id, start_date) values (1, 'g', '2026-10-01')")
    bootstrap.init(f"sqlite:///{db.as_posix()}")
    with sqlite3.connect(db) as c:
        assert c.execute("select days, trial_round from test_run").fetchone() == (14, 1)


def _spend(cny: float, at: datetime | None = None) -> None:
    with session_scope() as s:
        s.add(LlmUsage(agent="ops.review", model="claude-sonnet-5-5", cost_cny=cny, at=at or utcnow()))


def test_budget_alerts_once_per_level(env):
    services, gateway = env
    _spend(6.5)  # 日预算 8，提醒线 80%
    with session_scope() as s:
        assert check_budget_alerts(s, services, gateway) == ["日:warn"]
        assert check_budget_alerts(s, services, gateway) == []  # 同一级别只提醒一次
    _spend(2)
    with session_scope() as s:
        assert check_budget_alerts(s, services, gateway) == ["日:over"]
        try:
            gateway.check_budget(s, "ops.review")
            raise AssertionError("超预算应推迟非关键任务")
        except BudgetExceeded as e:
            assert "日预算" in str(e)
        gateway.check_budget(s, "ceo.daily")  # 关键环节照常
    assert "预算提醒" in services.feishu.sent[-1]["card"]["header"]["title"]["content"]


def test_monthly_budget_blocks_non_critical(env):
    services, gateway = env
    start = Gateway._month_start() + timedelta(minutes=1)
    if start > Gateway._day_start():  # 每月 1 日：月初就是今天，花费会先触发日预算
        return
    _spend(200, start)
    with session_scope() as s:
        assert gateway.over_budget(s) == "本月 LLM 花费已达月预算"
        assert "月:over" in check_budget_alerts(s, services, gateway)


def test_weekly_system_report(env):
    services, gateway = env
    with session_scope() as s:
        for i, st in enumerate(["success", "success", "missing", "manual_requested"]):
            s.add(FetchJob(item=f"i{i}", day=DAY - timedelta(days=i), status=st, channel="web"))
        s.add(LlmUsage(agent="ceo.daily", model="claude-opus-5-5", cost_cny=1.5, lane="main",
                       at=datetime(DAY.year, DAY.month, DAY.day, 2)))
        s.add(Job(type="x", status="failed", updated_at=datetime(DAY.year, DAY.month, DAY.day, 3)))
        r = weekly_system_report(s, DAY)
        c = r.content
        assert c["fetch"] == {"total": 4, "success": 2, "success_rate": 0.5, "manual": 0, "missing": 1}
        assert c["cost"]["main"] == 1.5 and list(c["cost"]["top_agents"][0]) == ["ceo.daily", 1.5]
        assert c["jobs_failed"] == 1 and c["applications"]["total"] == 15 and c["applications"]["pending"]
        enqueue(s, "system.weekly_report", {"end": DAY.isoformat()})
    run_until_idle(services, gateway)
    msg = services.feishu.sent[-1]
    assert msg["open_id"] == OWNER and "周度系统报告" in msg["card"]["header"]["title"]["content"]
    with session_scope() as s:
        assert s.query(Report).filter_by(kind="system").count() == 1


def _daily(services, gateway):
    backfill(services, DAY, 14)
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)


def test_daily_diagnosis_goes_through_batch(env):
    services, gateway = env
    _daily(services, gateway)
    with session_scope() as s:
        b = s.query(LlmBatch).one()
        assert b.status == "ended" and b.agent == "ops.diagnose" and "ops" in b.results
        assert s.query(LlmUsage).filter_by(agent="ops.diagnose", batch=True).count() == 1
        assert s.query(LlmUsage).filter_by(agent="ops.diagnose", batch=False).count() == 0  # 没有再走实时
        assert s.query(Report).filter_by(kind="daily").count() == 1
        assert s.query(Suggestion).filter_by(goods_id="g_haiyan").count() == 1


def test_batch_timeout_falls_back_to_realtime(env):
    services, gateway = env
    services.llm.batch_results = lambda _bid: None  # Batch 一直不返回
    _daily(services, gateway)
    with session_scope() as s:
        assert s.query(Report).count() == 0  # 还在等 Batch
        poll = s.query(Job).filter_by(type="llm.batch_poll", status="queued").one()
        assert poll.run_after > utcnow()  # 10 分钟后再查
        b = s.query(LlmBatch).one()
        b.deadline_at = poll.run_after = utcnow() - timedelta(seconds=1)
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert s.query(LlmBatch).one().status == "fallback"
        assert s.query(Report).filter_by(kind="daily").count() == 1
        assert s.query(LlmUsage).filter_by(agent="ops.diagnose", batch=False).count() == 1
