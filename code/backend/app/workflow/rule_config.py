"""规则配置的在线修改（02 号文档 7.2「规则配置」）：店主在 Web 改，每次保存一个版本（rule_config 表，key=rules）。

config/rules.yaml 是基线；最新版本整份覆盖到 get_settings().rules 上，web 与 worker 各自在用之前调用 apply_latest()
（worker 每个作业开始前查一次，只比较版本号，开销很小）。只允许改基线里已有的键，类型必须一致。
"""
from __future__ import annotations

import copy
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import RuleConfig

KEY = "rules"
_base: dict[str, Any] | None = None
_applied_id: int | None = None


class RuleError(ValueError):
    pass


def _baseline() -> dict[str, Any]:
    global _base
    if _base is None:
        _base = copy.deepcopy(get_settings().rules)
    return _base


def _replace(rules: dict[str, Any]) -> None:
    live = get_settings().rules
    live.clear()
    live.update(copy.deepcopy(rules))


def latest(s: Session) -> RuleConfig | None:
    return s.scalar(select(RuleConfig).where(RuleConfig.key == KEY).order_by(RuleConfig.id.desc()))


def apply_latest(s: Session) -> int:
    """把最新版本应用到内存里的规则；返回当前版本号（0 = 用的是 rules.yaml 基线）。"""
    global _applied_id
    base = _baseline()
    row = latest(s)
    row_id = row.id if row else None
    if row_id != _applied_id:
        _replace(_merge(base, row.value) if row else base)
        _applied_id = row_id
    return row.rule_version if row else 0


def _merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in patch.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def _check(base: Any, new: Any, path: str) -> None:
    if isinstance(base, dict):
        if not isinstance(new, dict):
            raise RuleError(f"{path} 应该是一组设置")
        for k in new:
            if k not in base:
                raise RuleError(f"不认识的设置项 {path}.{k}".lstrip("."))
            _check(base[k], new[k], f"{path}.{k}")
    elif isinstance(base, bool):
        if not isinstance(new, bool):
            raise RuleError(f"{path.lstrip('.')} 应该是 是/否")
    elif isinstance(base, (int, float)):
        if isinstance(new, bool) or not isinstance(new, (int, float)):
            raise RuleError(f"{path.lstrip('.')} 应该是数字")
        if new < 0:
            raise RuleError(f"{path.lstrip('.')} 不能是负数")
    elif isinstance(base, list):
        if not isinstance(new, list) or not all(isinstance(x, str) for x in new):
            raise RuleError(f"{path.lstrip('.')} 应该是一组文字")
    elif isinstance(base, str) and not isinstance(new, str):
        raise RuleError(f"{path.lstrip('.')} 应该是文字")


def save(s: Session, patch: dict[str, Any], by: str) -> RuleConfig:
    """校验并保存一个新版本（patch 只需要包含改动的部分）。"""
    base = _baseline()
    _check(base, patch, "")
    row = latest(s)
    merged = _merge(_merge(base, row.value) if row else base, patch)
    weights = merged["selection"]["score_weights"]
    if round(sum(weights.values()), 6) != 100:
        raise RuleError(f"打分权重合计应为 100，现在是 {sum(weights.values()):g}")
    if not 0 < merged["selection"]["hard_filters"]["min_margin"] < 1:
        raise RuleError("最低毛利率应在 0 到 1 之间（例如 0.25 表示 25%）")
    new = RuleConfig(key=KEY, value=merged, rule_version=(row.rule_version + 1) if row else 1, approved_by=by)
    s.add(new)
    s.flush()
    apply_latest(s)
    return new


def history(s: Session, limit: int = 20) -> list[RuleConfig]:
    return list(s.scalars(select(RuleConfig).where(RuleConfig.key == KEY).order_by(RuleConfig.id.desc()).limit(limit)))


def diff(old: dict[str, Any], new: dict[str, Any], path: str = "") -> list[str]:
    """两个版本之间改了哪些项，例如 selection.hard_filters.min_margin: 0.25 → 0.3。"""
    out = []
    for k in sorted(set(old) | set(new)):
        a, b, p = old.get(k), new.get(k), f"{path}.{k}" if path else k
        if isinstance(a, dict) and isinstance(b, dict):
            out += diff(a, b, p)
        elif a != b:
            out.append(f"{p}: {a} → {b}")
    return out
