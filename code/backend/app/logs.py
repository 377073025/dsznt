"""日志（07 号文档 4.3、14 节）：结构化 JSON、按天滚动保留 30 天、统一脱敏。

每条日志自动带上当前上下文（job_id / job_type / lane / agent），由 log_context() 设置。
控制台仍输出便于阅读的文本格式；文件 data/logs/<进程>.log 为 JSON，每行一条。
"""
from __future__ import annotations

import contextvars
import json
import logging
import re
from contextlib import contextmanager
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler
from typing import Any, Iterator

_CTX: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar("log_ctx", default={})
KEEP_DAYS = 30

# 脱敏：token / 密钥 / Cookie 类的键值、手机号、身份证号
_REDACT = [
    (re.compile(r"(?i)((?:access_?token|refresh_?token|access_?key|ticket|token|secret|api_?key|password|cookie|authorization)"
                r"[\"']?\s*[:=]\s*[\"']?)([^\s\"',;&]+)"), r"\1***"),
    (re.compile(r"(?i)(bearer\s+)[\w\-.]+"), r"\1***"),
    (re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)"), r"\1****\2"),
    (re.compile(r"(?<!\d)(\d{6})\d{8}(\d{3}[\dXx])(?!\d)"), r"\1********\2"),
]


def redact(text: str) -> str:
    for pattern, repl in _REDACT:
        text = pattern.sub(repl, text)
    return text


@contextmanager
def log_context(**fields: Any) -> Iterator[None]:
    token = _CTX.set({**_CTX.get(), **{k: v for k, v in fields.items() if v not in (None, "")}})
    try:
        yield
    finally:
        _CTX.reset(token)


class _ContextFilter(logging.Filter):
    """把上下文和脱敏后的消息放到记录上，文本与 JSON 两种格式共用。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.ctx = _CTX.get()
        record.msg, record.args = redact(record.getMessage()), ()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        d: dict[str, Any] = {
            "at": datetime.fromtimestamp(record.created, timezone.utc).isoformat(timespec="milliseconds"),
            "level": record.levelname, "logger": record.name, "msg": record.getMessage(),
            **getattr(record, "ctx", {}),
        }
        if record.exc_info:
            d["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(d, ensure_ascii=False, default=str)


class _TextFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        ctx = getattr(record, "ctx", {})
        line = super().format(record)
        return f"{line} {ctx}" if ctx else line


def setup_logging(process: str | None = None) -> None:
    """process 为 web / worker 时同时写 JSON 文件；测试和脚本只输出到控制台。重复调用无副作用。"""
    root = logging.getLogger()
    if getattr(root, "_dsznt_configured", None) == (process or ""):
        return
    for h in list(root.handlers):
        if getattr(h, "_dsznt", False):
            root.removeHandler(h)
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(_TextFormatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    handlers: list[logging.Handler] = [console]
    if process:
        from app.config import get_settings

        log_dir = get_settings().path("logs_dir")
        log_dir.mkdir(parents=True, exist_ok=True)
        f = TimedRotatingFileHandler(log_dir / f"{process}.log", when="midnight", backupCount=KEEP_DAYS, encoding="utf-8")
        f.setFormatter(JsonFormatter())
        handlers.append(f)
    for h in handlers:
        h._dsznt = True  # type: ignore[attr-defined]
        h.addFilter(_ContextFilter())
        root.addHandler(h)
    root._dsznt_configured = process or ""  # type: ignore[attr-defined]
