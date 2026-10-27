"""快照对比生成改动事件（07 号文档 6.7 节）。

每日商品快照对比前一天：标题、主图、价格、SKU 变化 → change_event(source=snapshot)。
同一改动已由系统任务登记（source=task）时，按商品 + 类型 + 48 小时窗口去重；重复执行同一天也不会重复生成。
"""
from __future__ import annotations

import json
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import ChangeEvent, ProductSnapshot

DEDUPE_WINDOW = timedelta(days=2)


def _fields(snap: ProductSnapshot) -> dict[str, str]:
    return {
        "title": snap.title,
        "main_image": snap.main_image_hash,
        "price": str(snap.price_fen),
        "sku": json.dumps(snap.skus, ensure_ascii=False, sort_keys=True),
    }


def detect_changes(s: Session, day: date) -> list[ChangeEvent]:
    prev = {p.goods_id: p for p in s.scalars(select(ProductSnapshot).where(ProductSnapshot.day == day - timedelta(days=1)))}
    created = []
    for cur in s.scalars(select(ProductSnapshot).where(ProductSnapshot.day == day)):
        before = prev.get(cur.goods_id)
        if before is None:
            continue
        old, new = _fields(before), _fields(cur)
        for kind in old:
            if old[kind] == new[kind] or _already_recorded(s, cur.goods_id, kind, day):
                continue
            ev = ChangeEvent(goods_id=cur.goods_id, type=kind, before=old[kind], after=new[kind], occurred_on=day, source="snapshot")
            s.add(ev)
            created.append(ev)
    s.flush()
    return created


def _already_recorded(s: Session, goods_id: str, kind: str, day: date) -> bool:
    evs = s.scalars(select(ChangeEvent).where(
        ChangeEvent.goods_id == goods_id, ChangeEvent.type == kind,
        ChangeEvent.occurred_on >= day - DEDUPE_WINDOW, ChangeEvent.occurred_on <= day,
    )).all()
    return any(e.source == "task" or e.occurred_on == day for e in evs)
