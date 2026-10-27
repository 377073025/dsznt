"""测试用页面：元素列表 + 表格内容 + 页面文本，实现 script.Page 接口。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class FakeElement:
    role: str
    name: str
    text: str = ""
    value: str = ""


@dataclass
class FakePage:
    elements: list[FakeElement] = field(default_factory=list)
    tables: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    text: str = ""
    url: str = ""
    clicks: list[str] = field(default_factory=list)

    def goto(self, url: str) -> None:
        self.url = url

    def find(self, role: str, name: str = "", name_contains: str = "") -> FakeElement | None:
        for e in self.elements:
            if e.role == role and (not name or e.name == name) and (not name_contains or name_contains in e.name):
                return e
        return None

    def click(self, el: FakeElement) -> None:
        self.clicks.append(el.name)

    def fill(self, el: FakeElement, value: str) -> None:
        el.value = value

    def read_text(self, el: FakeElement) -> str:
        return el.text or el.name

    def read_table(self, el: FakeElement) -> list[dict[str, Any]]:
        return [dict(r) for r in self.tables.get(el.name, [])]

    def snapshot(self) -> list[dict[str, str]]:
        return [{"role": e.role, "name": e.name} for e in self.elements]
