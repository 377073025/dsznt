"""数字一致性校验（07 号文档 7.3 节）。

组装输入时生成「事实表」：每个数值带来源键。模型输出中的每个数字必须
等于某个事实值（允许百分数换算），或由两个事实值做差 / 比值 / 变化率得出。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import permutations

_DATE_PATTERNS = [
    re.compile(r"\d{4}-\d{1,2}-\d{1,2}"),
    re.compile(r"\d{4}年\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{1,2}月\d{1,2}日"),
    re.compile(r"\d{1,2}/\d{1,2}"),
]
_NUMBER = re.compile(r"-?\d+(?:\.\d+)?")


@dataclass
class FactTable:
    values: dict[str, float] = field(default_factory=dict)
    # 文本里出现时不参与校验的片段（商品名、规格等，如「200g」）
    ignore_strings: set[str] = field(default_factory=set)

    def add(self, key: str, value: float | int) -> float:
        self.values[key] = float(value)
        return float(value)

    def as_prompt_lines(self) -> str:
        return "\n".join(f"{k} = {_fmt(v)}" for k, v in self.values.items())


def _fmt(v: float) -> str:
    return str(int(v)) if v == int(v) else f"{v:g}"


def _close(n: float, v: float) -> bool:
    # 只容忍保留一位小数的舍入误差；按比例放宽会让大数（访客 2491 ≈ 2486）也蒙混过关
    return abs(n - v) <= 0.051


def extract_numbers(text: str, ignore: set[str]) -> list[float]:
    for s in sorted(ignore, key=len, reverse=True):
        text = text.replace(s, " ")
    for p in _DATE_PATTERNS:
        text = p.sub(" ", text)
    return [float(m) for m in _NUMBER.findall(text)]


def check_numbers(text: str, facts: FactTable) -> list[float]:
    """返回不能被事实表解释的数字；空列表表示通过。

    派生值只在同组事实之间计算（同一对象的不同指标，或不同对象的同一指标），
    否则事实一多，几乎任何数字都能「凑」出来，校验就失去意义。
    """
    base = list(facts.values.values())
    candidates = set()
    for v in base:
        candidates.update((v, v * 100, abs(v)))
    groups = _groups(facts.values)
    unexplained = []
    for n in extract_numbers(text, facts.ignore_strings):
        if any(_close(n, c) for c in candidates):
            continue
        if any(_derived(n, g) for g in groups):
            continue
        unexplained.append(n)
    return unexplained


def _groups(values: dict[str, float]) -> list[list[float]]:
    by_prefix: dict[str, list[float]] = {}
    by_suffix: dict[str, list[float]] = {}
    for key, v in values.items():
        prefix, _, suffix = key.rpartition(".")
        by_prefix.setdefault(prefix, []).append(v)
        by_suffix.setdefault(suffix, []).append(v)
    return [g for g in (*by_prefix.values(), *by_suffix.values()) if len(g) > 1]


def _derived(n: float, base: list[float]) -> bool:
    for a, b in permutations(base, 2):
        diff = a - b
        if _close(n, diff) or _close(n, abs(diff)):
            return True
        if b:
            if _close(n, a / b) or _close(n, abs(diff / b * 100)):
                return True
    return False
