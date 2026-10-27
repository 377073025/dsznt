"""数据项定义（07 号文档 6.1 节）：config/items/*.yaml → ItemDef。"""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

import yaml

from app.config import CONFIG_DIR


@dataclass(frozen=True)
class ChannelDef:
    type: str                 # api / web / manual
    capability: str = ""      # api / web 通道对应的能力开关
    call: str = ""            # <服务命名空间>.<方法>，例如 pdd.list_goods
    script: str = ""          # 网页读取脚本名
    template: str = ""        # 人工导入：导出模板
    guide: str = ""           # 人工导入：图文指引


@dataclass(frozen=True)
class ItemDef:
    item: str
    schema: str
    required_fields: tuple[str, ...]
    deadline_minutes: int
    channels: tuple[ChannelDef, ...] = field(default_factory=tuple)

    def capabilities(self) -> set[str]:
        return {c.capability for c in self.channels if c.capability}


@lru_cache
def load_items() -> dict[str, ItemDef]:
    out: dict[str, ItemDef] = {}
    for path in sorted((CONFIG_DIR / "items").glob("*.yaml")):
        for raw in yaml.safe_load(path.read_text(encoding="utf-8")) or []:
            out[raw["item"]] = ItemDef(
                item=raw["item"],
                schema=raw["schema"],
                required_fields=tuple(raw.get("required_fields", [])),
                deadline_minutes=int(raw.get("deadline_minutes", 90)),
                channels=tuple(ChannelDef(**c) for c in raw["channels"]),
            )
    return out
