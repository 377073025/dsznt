"""看门狗（07 号文档 2.2 节）：启动 web 与 worker 两个进程，任何一个退出都在几秒内重启。

用法（在 code/backend 目录）：
    .venv/Scripts/python scripts/watchdog.py              # 前台运行，Ctrl+C 退出并停止两个进程
开机自启见 scripts/install_autostart.ps1（Windows 计划任务调用本脚本）。

连续快速崩溃时逐步拉长重启间隔（最长 5 分钟），避免配置错误时无限刷日志；稳定运行 10 分钟后间隔复位。
"""
from __future__ import annotations

import argparse
import logging
import subprocess
import sys
import time
from dataclasses import dataclass, field
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
RESTART_DELAY = 3          # 秒；崩溃后 30 秒内必须恢复
MAX_DELAY = 300
STABLE_AFTER = 600

log = logging.getLogger("watchdog")


@dataclass
class Proc:
    name: str
    cmd: list[str]
    p: subprocess.Popen | None = None
    started: float = 0.0
    delay: float = RESTART_DELAY
    next_start: float = 0.0
    restarts: int = field(default=0)

    def start(self) -> None:
        self.p = subprocess.Popen(self.cmd, cwd=BACKEND)
        self.started = time.monotonic()
        log.info("started %s (pid %s): %s", self.name, self.p.pid, " ".join(self.cmd))

    def check(self) -> None:
        now = time.monotonic()
        if self.p is None:
            if now >= self.next_start:
                self.start()
            return
        code = self.p.poll()
        if code is None:
            if now - self.started > STABLE_AFTER:
                self.delay = RESTART_DELAY
            return
        self.restarts += 1
        quick = now - self.started < 60
        self.delay = min(self.delay * 2, MAX_DELAY) if quick else RESTART_DELAY
        log.warning("%s exited with code %s, restart #%s in %ss", self.name, code, self.restarts, self.delay)
        self.p, self.next_start = None, now + self.delay

    def stop(self) -> None:
        if self.p is not None and self.p.poll() is None:
            self.p.terminate()
            try:
                self.p.wait(15)
            except subprocess.TimeoutExpired:
                self.p.kill()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", default="8000")
    args = ap.parse_args()

    log_dir = BACKEND / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handler = TimedRotatingFileHandler(log_dir / "watchdog.log", when="midnight", backupCount=30, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s",
                        handlers=[logging.StreamHandler(), handler])

    py = sys.executable
    procs = [
        Proc("worker", [py, "-m", "app.worker"]),
        Proc("web", [py, "-m", "uvicorn", "app.main:app", "--host", args.host, "--port", args.port]),
    ]
    try:
        while True:
            for p in procs:
                p.check()
            time.sleep(2)
    except KeyboardInterrupt:
        log.info("stopping")
    finally:
        for p in procs:
            p.stop()


if __name__ == "__main__":
    main()
