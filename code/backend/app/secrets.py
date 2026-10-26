"""凭证存储：Windows 凭据管理器（keyring）。数据库只存引用名。

测试与模拟模式可设 DSZNT_SECRETS=memory，改用进程内存储。
"""
from __future__ import annotations

import os

SERVICE = "dsznt"
_memory: dict[str, str] = {}


def _use_memory() -> bool:
    return os.environ.get("DSZNT_SECRETS") == "memory"


def set_secret(ref: str, value: str) -> None:
    if _use_memory():
        _memory[ref] = value
        return
    import keyring

    keyring.set_password(SERVICE, ref, value)


def get_secret(ref: str) -> str | None:
    if _use_memory():
        return _memory.get(ref)
    import keyring

    return keyring.get_password(SERVICE, ref)


def mask(value: str) -> str:
    """日志脱敏：只保留首尾各 2 位。"""
    return value[:2] + "***" + value[-2:] if len(value) > 6 else "***"
