"""每平台限速（07 号文档 6.5 节）：两次翻页之间至少间隔 min_interval_s（有 max_interval_s 时取随机间隔），
每天页面数达到 daily_pages 后当天停止。参数来自 config/platforms.yaml。

单独任务（lane=adhoc）最多用每日页数的 adhoc.web_page_ratio（17.4 节），和主流程共用总上限。"""
from __future__ import annotations

import random
import time
from datetime import datetime, timedelta
from typing import Callable

from app.config import get_settings
from app.data.errors import DailyLimitReached

__all__ = ["DailyLimitReached", "RateLimiter", "get_limiter"]


class RateLimiter:
    def __init__(self, platform: str, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] = datetime.now):
        conf = get_settings().platforms[platform]["rate_limit"]
        self.platform = platform
        self.min_interval = float(conf["min_interval_s"])
        self.max_interval = float(conf.get("max_interval_s", conf["min_interval_s"]))
        self.daily_pages = int(conf["daily_pages"])
        self._sleep, self._clock = sleep, clock
        self.adhoc_pages = int(self.daily_pages * get_settings().adhoc["web_page_ratio"])
        self._last: datetime | None = None
        self._day = clock().date()
        self.used_today = 0
        self.adhoc_used_today = 0

    def _interval(self) -> float:
        if self.max_interval <= self.min_interval:
            return self.min_interval
        return random.uniform(self.min_interval, self.max_interval)

    def acquire(self, lane: str = "main") -> None:
        """翻一页之前调用：必要时等待；当天额度用完抛 DailyLimitReached。"""
        now = self._clock()
        if now.date() != self._day:
            self._day, self.used_today, self.adhoc_used_today = now.date(), 0, 0
        if self.used_today >= self.daily_pages:
            raise DailyLimitReached(f"{self.platform} 今日已读取 {self.used_today} 页，达到上限")
        if lane == "adhoc" and self.adhoc_used_today >= self.adhoc_pages:
            raise DailyLimitReached(f"{self.platform} 今日单独任务已读取 {self.adhoc_used_today} 页，达到单独任务额度")
        wait = 0.0
        if self._last is not None:
            wait = self._interval() - (now - self._last).total_seconds()
            if wait > 0:
                self._sleep(wait)
        self._last = now + timedelta(seconds=max(wait, 0.0))
        self.used_today += 1
        if lane == "adhoc":
            self.adhoc_used_today += 1


_limiters: dict[str, RateLimiter] = {}


def get_limiter(platform: str) -> RateLimiter:
    """同一进程内每个平台共用一个限速器。"""
    if platform not in _limiters:
        _limiters[platform] = RateLimiter(platform)
    return _limiters[platform]
