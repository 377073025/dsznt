"""能力开关（07 号文档第 4 节）：申请下来一项打开一项，没下来的自动走降级。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import Application, Capability, CapabilityEvent

NOT_CONFIGURED, CONFIGURED, HEALTHY, DEGRADED, BLOCKED = (
    "not_configured", "configured", "healthy", "degraded", "blocked",
)
FAILS_BEFORE_DEGRADED = 3


@dataclass(frozen=True)
class CapabilityDef:
    key: str
    applications: tuple[str, ...]
    title: str
    required: bool


CAPABILITIES: dict[str, CapabilityDef] = {
    c.key: c
    for c in [
        CapabilityDef("llm", ("R06",), "模型服务", True),
        CapabilityDef("feishu_bot", ("R04", "R05"), "飞书机器人", True),
        CapabilityDef("pdd_web", ("R01", "R13"), "拼多多后台网页读取", True),
        CapabilityDef("pdd_api", ("R07",), "拼多多开放平台", False),
        CapabilityDef("alibaba_web", ("R02", "R13"), "1688 网页读取", True),
        CapabilityDef("alibaba_api", ("R08",), "1688 开放平台", False),
        CapabilityDef("market_tool", ("R10",), "市场数据工具", False),
        CapabilityDef("dropship_tool", ("R03", "R09"), "铺货代发工具", True),
        CapabilityDef("image_gen", ("R11",), "生图服务", False),
        CapabilityDef("tunnel", ("R12",), "内网穿透", False),
    ]
}

# 08 号人工申请清单
APPLICATIONS: list[tuple[str, str, str]] = [
    ("R01", "拼多多店铺与主账号", "必需"),
    ("R02", "1688 采集账号（独立）", "必需"),
    ("R03", "1688 采购账号（代发付款用）", "必需"),
    ("R04", "飞书企业 / 团队", "必需"),
    ("R05", "飞书自建应用（机器人）", "必需"),
    ("R06", "LLM 模型 API 账号", "必需"),
    ("R07", "拼多多开放平台开发者与应用", "增强"),
    ("R08", "1688 开放平台开发者与 API 权限", "增强"),
    ("R09", "铺货代发工具", "必需"),
    ("R10", "市场数据工具", "增强"),
    ("R11", "生图服务 API", "增强"),
    ("R12", "内网穿透", "备用"),
    ("R13", "浏览器登录（拼多多后台、1688）", "必需"),
    ("R14", "本地主机与运行环境", "必需"),
    ("R15", "首批类目确定与店铺类目开通", "必需"),
]

# 模拟模式下的初始状态：开放平台按「大概率不批」处理，用来演示降级
FAKE_MODE_STATUS = {
    "llm": HEALTHY, "feishu_bot": HEALTHY, "pdd_web": HEALTHY, "pdd_api": NOT_CONFIGURED,
    "alibaba_web": HEALTHY, "alibaba_api": NOT_CONFIGURED, "market_tool": HEALTHY,
    "dropship_tool": HEALTHY, "image_gen": HEALTHY, "tunnel": NOT_CONFIGURED,
}


def seed(session: Session, fake_mode: bool) -> None:
    for app_id, name, level in APPLICATIONS:
        if session.get(Application, app_id) is None:
            session.add(Application(id=app_id, name=name, level=level))
    for key, d in CAPABILITIES.items():
        if session.get(Capability, key) is None:
            status = FAKE_MODE_STATUS[key] if fake_mode else NOT_CONFIGURED
            session.add(Capability(key=key, status=status, application_ids=list(d.applications)))
    session.flush()


def is_healthy(session: Session, key: str) -> bool:
    cap = session.get(Capability, key)
    return cap is not None and cap.status == HEALTHY


def set_status(session: Session, key: str, status: str, reason: str = "") -> None:
    cap = session.get(Capability, key)
    assert cap is not None, f"unknown capability {key}"
    if cap.status == status:
        return
    session.add(CapabilityEvent(key=key, from_status=cap.status, to_status=status, reason=reason))
    cap.status = status
    if status == HEALTHY:
        cap.fail_count = 0


def mark_configured(session: Session, key: str, credential_ref: str) -> None:
    cap = session.get(Capability, key)
    assert cap is not None
    cap.credential_ref = credential_ref
    set_status(session, key, CONFIGURED, "凭证已录入")


def run_health_checks(session: Session, checks: dict[str, Callable[[], None]]) -> list[tuple[str, str, str]]:
    """对已配置的能力逐个检查；返回状态变化列表 (key, from, to)，供通知服务推送。"""
    changes = []
    caps = session.scalars(select(Capability).where(Capability.status != NOT_CONFIGURED)).all()
    for cap in caps:
        if cap.status == BLOCKED or cap.key not in checks:
            continue  # blocked 需要人工恢复
        before = cap.status
        cap.last_check_at = utcnow()
        try:
            checks[cap.key]()
            set_status(session, cap.key, HEALTHY, "健康检查通过")
        except Exception as e:  # noqa: BLE001  任何失败都计数
            cap.fail_count += 1
            cap.last_error = str(e)[:500]
            if cap.fail_count >= FAILS_BEFORE_DEGRADED:
                set_status(session, cap.key, DEGRADED, f"连续 {cap.fail_count} 次检查失败")
        if cap.status != before:
            changes.append((cap.key, before, cap.status))
    return changes
