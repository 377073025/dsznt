"""业务表（02 号文档第 6 节）。金额统一用「分」存整数。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base, TimestampMixin, utcnow


class Product(Base, TimestampMixin):
    __tablename__ = "product"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shop.id"))
    goods_id: Mapped[str] = mapped_column(String(64), unique=True)  # 平台商品 ID
    name: Mapped[str] = mapped_column(String(128))
    series: Mapped[str] = mapped_column(String(64), default="")
    title: Mapped[str] = mapped_column(String(256), default="")
    price_fen: Mapped[int] = mapped_column(Integer, default=0)
    main_image_hash: Mapped[str] = mapped_column(String(64), default="")
    source_offer_id: Mapped[str] = mapped_column(String(64), default="")


class ProductSnapshot(Base):
    __tablename__ = "product_snapshot"
    __table_args__ = (UniqueConstraint("goods_id", "day", name="uq_snapshot_goods_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    goods_id: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    title: Mapped[str] = mapped_column(String(256))
    main_image_hash: Mapped[str] = mapped_column(String(64))
    price_fen: Mapped[int] = mapped_column(Integer)
    skus: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    source: Mapped[str] = mapped_column(String(16), default="")


class DailyMetric(Base):
    __tablename__ = "daily_metric"
    __table_args__ = (UniqueConstraint("goods_id", "day", name="uq_metric_goods_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    goods_id: Mapped[str] = mapped_column(String(64))
    day: Mapped[date] = mapped_column(Date)
    visitors: Mapped[int] = mapped_column(Integer, default=0)
    add_to_cart: Mapped[int] = mapped_column(Integer, default=0)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    gmv_fen: Mapped[int] = mapped_column(Integer, default=0)
    refunds: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="")


class ShopDailyMetric(Base):
    """店铺每日汇总（order_stats 数据项）。"""

    __tablename__ = "shop_daily_metric"
    __table_args__ = (UniqueConstraint("day", name="uq_shop_metric_day"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    day: Mapped[date] = mapped_column(Date)
    orders: Mapped[int] = mapped_column(Integer, default=0)
    gmv_fen: Mapped[int] = mapped_column(Integer, default=0)
    refunds: Mapped[int] = mapped_column(Integer, default=0)
    source: Mapped[str] = mapped_column(String(16), default="")


class ChangeEvent(Base):
    __tablename__ = "change_event"
    id: Mapped[int] = mapped_column(primary_key=True)
    goods_id: Mapped[str] = mapped_column(String(64))
    type: Mapped[str] = mapped_column(String(32))  # title / main_image / price / sku
    before: Mapped[str] = mapped_column(Text, default="")
    after: Mapped[str] = mapped_column(Text, default="")
    occurred_on: Mapped[date] = mapped_column(Date)
    source: Mapped[str] = mapped_column(String(16))  # task / snapshot / manual
    task_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Asset(Base, TimestampMixin):
    __tablename__ = "asset"
    id: Mapped[int] = mapped_column(primary_key=True)
    goods_id: Mapped[str] = mapped_column(String(64), default="")
    kind: Mapped[str] = mapped_column(String(16))  # main / detail / scene / sku
    path: Mapped[str] = mapped_column(String(512))
    asset_version: Mapped[int] = mapped_column(Integer, default=1)
    origin: Mapped[str] = mapped_column(String(16))  # supplier / ai / edited
    note: Mapped[str] = mapped_column(Text, default="")  # 来源说明，例如 任务 #3：换成卧室场景


class Demand(Base, TimestampMixin):
    __tablename__ = "demand"
    id: Mapped[int] = mapped_column(primary_key=True)
    week: Mapped[str] = mapped_column(String(10))  # 2026-W43
    keyword: Mapped[str] = mapped_column(String(64))
    seed_type: Mapped[str] = mapped_column(String(16))  # extension / opportunity / replace
    seed_ref: Mapped[str] = mapped_column(String(64), default="")
    heat: Mapped[float] = mapped_column(Float, default=0.0)
    trend: Mapped[float] = mapped_column(Float, default=0.0)
    competitors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Offer(Base, TimestampMixin):
    __tablename__ = "offer"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # 1688 offer id
    title: Mapped[str] = mapped_column(String(256))
    category: Mapped[str] = mapped_column(String(64), default="")
    price_fen: Mapped[int] = mapped_column(Integer)
    shipping_fen: Mapped[int] = mapped_column(Integer, default=0)
    dropship: Mapped[bool] = mapped_column(default=False)
    dropship_auto: Mapped[str] = mapped_column(String(8), default="unknown")  # yes / no / unknown
    stock: Mapped[int] = mapped_column(Integer, default=0)
    ship_48h_rate: Mapped[float] = mapped_column(Float, default=0.0)
    repurchase_rate: Mapped[float] = mapped_column(Float, default=0.0)
    return_rate: Mapped[float] = mapped_column(Float, default=0.0)
    supplier_years: Mapped[int] = mapped_column(Integer, default=0)
    weight_kg: Mapped[float] = mapped_column(Float, default=0.0)
    longest_side_cm: Mapped[float] = mapped_column(Float, default=0.0)
    image_urls: Mapped[list[str]] = mapped_column(JSON, default=list)
    raw: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)


class Candidate(Base, TimestampMixin):
    __tablename__ = "candidate"
    id: Mapped[int] = mapped_column(primary_key=True)
    demand_id: Mapped[int] = mapped_column(ForeignKey("demand.id"))
    offer_id: Mapped[str] = mapped_column(ForeignKey("offer.id"))
    backup_offer_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    same_conf: Mapped[float] = mapped_column(Float, default=0.0)
    compliance: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    price_plan: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    score: Mapped[float] = mapped_column(Float, default=0.0)
    breakdown: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    reason: Mapped[str] = mapped_column(Text, default="")
    risks: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="discovered")
    reject_reason: Mapped[str] = mapped_column(Text, default="")
    revise_rounds: Mapped[int] = mapped_column(Integer, default=0)


class TestRun(Base, TimestampMixin):
    __tablename__ = "test_run"
    __test__ = False  # 不让 pytest 当成测试类收集
    id: Mapped[int] = mapped_column(primary_key=True)
    candidate_id: Mapped[int] = mapped_column(ForeignKey("candidate.id"))
    goods_id: Mapped[str] = mapped_column(String(64))
    start_date: Mapped[date] = mapped_column(Date)
    days: Mapped[int] = mapped_column(Integer, default=14)
    trial_round: Mapped[int] = mapped_column(Integer, default=1)  # 2 = 优化后的再观察
    visitors: Mapped[int] = mapped_column(Integer, default=0)
    cvr: Mapped[float] = mapped_column(Float, default=0.0)
    verdict: Mapped[str] = mapped_column(String(16), default="")  # keep / optimize / drop


class Suggestion(Base, TimestampMixin):
    __tablename__ = "suggestion"
    id: Mapped[int] = mapped_column(primary_key=True)
    shop_id: Mapped[int] = mapped_column(ForeignKey("shop.id"))
    goods_id: Mapped[str] = mapped_column(String(64), default="")
    day: Mapped[date] = mapped_column(Date)
    type: Mapped[str] = mapped_column(String(32))  # main_image / title / price / promotion / other
    action: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    evidence: Mapped[list[str]] = mapped_column(JSON, default=list)
    confidence: Mapped[str] = mapped_column(String(8), default="medium")
    needs_write: Mapped[bool] = mapped_column(default=False)
    status: Mapped[str] = mapped_column(String(16), default="generated")
    revise_rounds: Mapped[int] = mapped_column(Integer, default=0)


class Task(Base, TimestampMixin):
    __tablename__ = "task"
    id: Mapped[int] = mapped_column(primary_key=True)
    ref_type: Mapped[str] = mapped_column(String(16))  # suggestion / candidate
    ref_id: Mapped[int] = mapped_column(Integer)
    executor: Mapped[str] = mapped_column(String(32))  # copy / image / listing
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    needs_write: Mapped[bool] = mapped_column(default=False)
    checklist: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(24), default="pending")
    rework_count: Mapped[int] = mapped_column(Integer, default=0)
    output: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    write_confirmed_by: Mapped[str] = mapped_column(String(64), default="")


class Acceptance(Base):
    __tablename__ = "acceptance"
    id: Mapped[int] = mapped_column(primary_key=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("task.id"))
    checklist: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    passed: Mapped[bool] = mapped_column(default=False)
    comment: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Review(Base):
    __tablename__ = "review"
    id: Mapped[int] = mapped_column(primary_key=True)
    change_event_id: Mapped[int] = mapped_column(ForeignKey("change_event.id"))
    window_days: Mapped[int] = mapped_column(Integer)
    before: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    after: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    verdict: Mapped[str] = mapped_column(Text, default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class Report(Base):
    __tablename__ = "report"
    __table_args__ = (UniqueConstraint("kind", "period", name="uq_report_kind_period"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))  # daily / weekly
    period: Mapped[str] = mapped_column(String(16))  # 2026-10-24 / 2026-W43
    content: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    feishu_message_id: Mapped[str] = mapped_column(String(64), default="")
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
