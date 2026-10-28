"""浏览器互斥（07 号文档 17.4 节）：每个平台同一时刻只有一个任务用浏览器。

主流程拿锁时一直等（单独任务按页释放，最多等一页）；单独任务最多等 browser_wait_minutes，超时顺延。
"""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from typing import Iterator

from app.config import get_settings

_locks: dict[str, threading.RLock] = {}
_guard = threading.Lock()


def _lock(platform: str) -> threading.RLock:
    """可重入：单独任务先占住平台浏览器，任务里的适配器再按页拿锁时不会把自己锁死。"""
    with _guard:
        return _locks.setdefault(platform, threading.RLock())


class BrowserBusy(Exception):
    pass


@contextmanager
def browser_lock(platform: str, lane: str = "main") -> Iterator[None]:
    lock = _lock(platform)
    timeout = -1 if lane == "main" else get_settings().adhoc["browser_wait_minutes"] * 60
    if not lock.acquire(timeout=timeout):
        raise BrowserBusy(f"{platform} 浏览器被占用超过 {timeout // 60:.0f} 分钟")
    try:
        yield
    finally:
        lock.release()


def blackout_end(now_cn: datetime) -> datetime | None:
    """北京时间 now_cn 处于主流程采集时段（adhoc.web_blackout）内时，返回时段结束时刻；否则 None。"""
    start_s, end_s = get_settings().adhoc["web_blackout"].split("-")
    start, end = time.fromisoformat(start_s), time.fromisoformat(end_s)
    if start <= now_cn.time() < end:
        return datetime.combine(now_cn.date(), end)
    return None


def to_utc(cn: datetime) -> datetime:
    return cn - timedelta(hours=8)
