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
from app.bootstrap import init
from app.config import get_settings
from app.db import session_scope, utcnow
from app.llm.gateway import Gateway
from app.services import get_services
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


def _health_check() -> None:
    services = get_services()
    with session_scope() as s:
        for key, before, after in caps.run_health_checks(s, services.health_checks()):
            log.warning("capability %s: %s → %s", key, before, after)


def build_scheduler() -> BackgroundScheduler:
    conf = get_settings().raw["schedule"]
    tz = get_settings().raw["timezone"]
    sched = BackgroundScheduler(timezone=tz)
    sched.add_job(_enqueue_daily_fetch, CronTrigger.from_crontab(conf["fetch_daily"], timezone=tz), id="daily_fetch")
    sched.add_job(_enqueue_selection_weekly, CronTrigger.from_crontab(conf["selection_weekly"], timezone=tz), id="selection")
    sched.add_job(_health_check, "interval", minutes=conf["health_check_minutes"], id="health")
    sched.add_job(_enqueue_timeouts, "interval", minutes=30, id="timeouts")
    return sched


def main() -> None:
    init()
    recovered = recover_stale()
    if recovered:
        log.info("recovered %s stale jobs", recovered)
    services = get_services()
    gateway = Gateway(services.llm)
    sched = build_scheduler()
    sched.start()
    log.info("worker started (mode=%s)", services.mode)

    def job_loop() -> None:
        while True:
            try:
                if not run_until_idle(services, gateway, max_rounds=50):
                    time.sleep(1)
            except Exception:  # noqa: BLE001  单轮出错不让循环退出
                log.exception("job loop error")
                time.sleep(5)

    try:
        if services.mode == "real" and services.feishu is not None:
            # 飞书长连接必须在主线程；作业循环放后台线程
            from app.interfaces.feishu.longconn import run_forever
            from app.secrets import get_secret

            threading.Thread(target=job_loop, name="jobs", daemon=True).start()
            run_forever(get_secret("feishu_app_id"), get_secret("feishu_app_secret"))
        else:
            job_loop()
    except KeyboardInterrupt:
        sched.shutdown()


if __name__ == "__main__":
    main()
