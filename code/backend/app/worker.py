"""worker 进程：调度器 + 作业执行 + 事件分发（飞书长连接与浏览器池在 M1/M3 接入）。

运行：python -m app.worker
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app import capabilities as caps
from app.backup import run_backup
from app.bootstrap import init
from app.config import get_settings
from app.db import session_scope, utcnow
from app.llm.gateway import Gateway
from app.services import get_services
from app.agents.tracking import purge_old_runs, recover_stale_runs
from app.workflow.jobs import enqueue, recover_stale, run_until_idle

log = logging.getLogger("worker")


def _today_cn() -> date:
    return (utcnow() + timedelta(hours=8)).date()


def _enqueue_daily_fetch() -> None:
    day = _today_cn() - timedelta(days=1)  # 采集昨天的数据
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": day.isoformat()}, dedupe_key=f"daily.fetch:{day.isoformat()}")


def _enqueue_selection_weekly() -> None:
    iso = _today_cn().isocalendar()
    week = f"{iso.year}-W{iso.week:02d}"
    category = get_settings().rules.get("first_category", "香薰蜡烛")
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": week, "category": category}, dedupe_key=f"selection.weekly:{week}")


def _enqueue_timeouts() -> None:
    with session_scope() as s:
        enqueue(s, "system.timeouts", {})
        enqueue(s, "system.budget_check", {})


def _enqueue_system_report() -> None:
    end = _today_cn()
    with session_scope() as s:
        enqueue(s, "system.weekly_report", {"end": end.isoformat()}, dedupe_key=f"system.weekly_report:{end.isoformat()}")


def _enqueue_offer_watch() -> None:
    day = _today_cn().isoformat()
    with session_scope() as s:
        enqueue(s, "fetch.offer_watch", {"day": day}, dedupe_key=f"fetch.offer_watch:{day}")


def _enqueue_monthly_tuning() -> None:
    period = _today_cn().strftime("%Y-%m")
    with session_scope() as s:
        enqueue(s, "rules.monthly_tuning", {"period": period}, dedupe_key=f"rules.monthly_tuning:{period}")


def _backup() -> None:
    """在调度器线程里直接跑（不占作业通道）；失败时通过作业发飞书通知。"""
    try:
        out = run_backup()
        log.info("backup done: %s", out)
    except Exception as e:  # noqa: BLE001
        log.exception("backup failed")
        with session_scope() as s:
            enqueue(s, "system.notify", {"title": "备份失败", "body": f"{type(e).__name__}: {e}"})


def _health_check() -> None:
    services = get_services()
    with session_scope() as s:
        for key, before, after in caps.run_health_checks(s, services.health_checks()):
            log.warning("capability %s: %s → %s", key, before, after)


def _purge_runs() -> None:
    n_runs, n_steps = purge_old_runs()
    if n_runs or n_steps:
        log.info("purged %s agent runs, %s steps", n_runs, n_steps)


def build_scheduler() -> BackgroundScheduler:
    conf = get_settings().raw["schedule"]
    tz = get_settings().raw["timezone"]
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(_enqueue_daily_fetch, CronTrigger.from_crontab(conf["fetch_daily"], timezone=tz), id="daily_fetch")
    sched.add_job(_enqueue_selection_weekly, CronTrigger.from_crontab(conf["selection_weekly"], timezone=tz), id="selection")
    sched.add_job(_health_check, "interval", minutes=conf["health_check_minutes"], id="health")
    sched.add_job(_enqueue_timeouts, "interval", minutes=30, id="timeouts")
    sched.add_job(_backup, CronTrigger.from_crontab(conf["backup"], timezone=tz), id="backup")
    sched.add_job(_enqueue_system_report, CronTrigger.from_crontab(conf["system_report"], timezone=tz), id="system_report")
    sched.add_job(_enqueue_offer_watch, CronTrigger.from_crontab(conf["offer_watch"], timezone=tz), id="offer_watch")
    sched.add_job(_enqueue_monthly_tuning, CronTrigger.from_crontab(conf["monthly_tuning"], timezone=tz), id="tuning")
    sched.add_job(_purge_runs, CronTrigger.from_crontab(conf["purge_runs"], timezone=tz), id="purge_runs")
    return sched


def main() -> None:
    init(process="worker")
    recovered = recover_stale()
    if recovered:
        log.info("recovered %s stale jobs", recovered)
    _recover_adhoc()
    services = get_services()
    gateway = Gateway(services.llm)
    sched = build_scheduler()
    sched.start()
    log.info("worker started (mode=%s)", services.mode)

    def job_loop(lane: str) -> None:
        """两个通道各一个循环（07 号文档 17.4 节）：主流程作业永远不排在单独任务后面。事件只由主通道分发。"""
        while True:
            try:
                if not run_until_idle(services, gateway, max_rounds=50, lanes=(lane,), dispatch=lane == "main"):
                    time.sleep(1)
            except Exception:  # noqa: BLE001  单轮出错不让循环退出
                log.exception("job loop error (%s)", lane)
                time.sleep(5)

    # 单独通道同一时间只跑 1 个（adhoc.max_concurrent），一个线程即可保证
    threading.Thread(target=job_loop, args=("adhoc",), name="jobs-adhoc", daemon=True).start()
    try:
        if services.mode == "real" and services.feishu is not None:
            # 飞书长连接必须在主线程；作业循环放后台线程
            from app.interfaces.feishu.longconn import run_forever
            from app.secrets import get_secret

            threading.Thread(target=job_loop, args=("main",), name="jobs-main", daemon=True).start()
            run_forever(get_secret("feishu_app_id"), get_secret("feishu_app_secret"))
        else:
            job_loop("main")
    except KeyboardInterrupt:
        sched.shutdown()


def _recover_adhoc() -> None:
    """重启时：中断的运行记录标记失败；运行中的单独任务标记失败，可重新提交（不自动重跑）。"""
    from app.models import AdhocTask

    n = recover_stale_runs()
    with session_scope() as s:
        for t in s.query(AdhocTask).filter(AdhocTask.status == "running"):
            t.status, t.error = "failed", "worker 重启，运行中断，可重新提交"
    if n:
        log.info("marked %s interrupted agent runs as failed", n)


if __name__ == "__main__":
    main()
