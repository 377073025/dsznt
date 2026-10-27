"""只读查询：智能体、工具、复盘共用。返回纯数据结构，不在这里做业务判断。"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import utcnow
from app.models import ChangeEvent, DailyMetric, Product
from app.rules.metrics import DayRow


def day_rows(s: Session, end: date, days: int) -> list[DayRow]:
    """截至 end（含）的近 days 天、全部商品的每日指标。"""
    start = end - timedelta(days=days - 1)
    rows = s.scalars(
        select(DailyMetric).where(DailyMetric.day >= start, DailyMetric.day <= end).order_by(DailyMetric.day, DailyMetric.goods_id)
    )
    return [DayRow(r.goods_id, r.day, r.visitors, r.add_to_cart, r.orders, r.gmv_fen) for r in rows]


def products(s: Session) -> list[Product]:
    return list(s.scalars(select(Product).order_by(Product.id)))


def change_events(s: Session, goods_id: str, since: date) -> list[ChangeEvent]:
    return list(s.scalars(
        select(ChangeEvent).where(ChangeEvent.goods_id == goods_id, ChangeEvent.occurred_on >= since)
        .order_by(ChangeEvent.occurred_on, ChangeEvent.id)
    ))


def business_day(s: Session) -> date:
    """业务日期：已入库的最新一天每日指标；还没有数据时取北京时间昨天。"""
    latest = s.scalar(select(func.max(DailyMetric.day)))
    if latest is not None:
        return latest
    return (utcnow() + timedelta(hours=8)).date() - timedelta(days=1)
