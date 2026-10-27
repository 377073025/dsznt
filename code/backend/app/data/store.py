"""标准化 → 校验 → 入库（07 号文档 6.2 节）。各通道取到的数据都先转成 adapters/types 里的结构，再走这里。

入库按 (数据项, 主键, 日期) 幂等覆盖，source 记录来源（api / web / manual）。
"""
from __future__ import annotations

from datetime import date
from typing import Any, Iterable

from pydantic import BaseModel, ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.adapters import types as T
from app.data.errors import InvalidData
from app.data.items import ItemDef
from app.models import DailyMetric, Product, ProductSnapshot, Shop, ShopDailyMetric

_INT_FIELDS = ("visitors", "add_to_cart", "orders", "gmv_fen", "refunds", "price_fen")


def normalize(item: ItemDef, raw: Any) -> list[BaseModel]:
    """适配器返回单个对象或列表、模型或字典，统一成数据项 schema 的模型列表。"""
    schema: type[BaseModel] = getattr(T, item.schema)
    seq = raw if isinstance(raw, list) else [raw]
    out = []
    for i, r in enumerate(seq):
        try:
            out.append(r if isinstance(r, schema) else schema.model_validate(r.model_dump() if isinstance(r, BaseModel) else r))
        except ValidationError as e:
            raise InvalidData(f"第 {i + 1} 条结构不对：{e.errors()[0]['msg']}") from e
    return out


def check_record(item: ItemDef, rec: BaseModel) -> str | None:
    """单条记录的合理性校验，返回错误说明；通过返回 None。"""
    for f in item.required_fields:
        if getattr(rec, f, None) in (None, ""):
            return f"缺少 {f}"
    for f in _INT_FIELDS:
        v = getattr(rec, f, None)
        if isinstance(v, int) and v < 0:
            return f"{f} 不能为负数"
    visitors, orders = getattr(rec, "visitors", None), getattr(rec, "orders", None)
    if visitors is not None and orders is not None and orders > visitors:
        return f"下单数 {orders} 大于访客数 {visitors}"
    return None


def validate(item: ItemDef, records: list[BaseModel]) -> None:
    if not records and item.item != "order_stats":
        raise InvalidData("没有取到任何数据")
    for i, rec in enumerate(records):
        err = check_record(item, rec)
        if err:
            raise InvalidData(f"第 {i + 1} 条：{err}")


def save(s: Session, shop: Shop, item: ItemDef, day: date, records: Iterable[BaseModel], source: str) -> int:
    records = list(records)
    saver = _SAVERS.get(item.item)
    if saver is None:
        raise InvalidData(f"数据项 {item.item} 没有入库实现")
    for rec in records:
        saver(s, shop, day, rec, source)
    s.flush()
    return len(records)


def _upsert(s: Session, model: type, keys: dict[str, Any], values: dict[str, Any]) -> Any:
    obj = s.scalar(select(model).filter_by(**keys))
    if obj is None:
        obj = model(**keys)
        s.add(obj)
    for k, v in values.items():
        setattr(obj, k, v)
    return obj


def _save_goods(s: Session, shop: Shop, day: date, g: T.GoodsInfo, source: str) -> None:
    _upsert(s, ProductSnapshot, {"goods_id": g.goods_id, "day": day}, {
        "title": g.title, "main_image_hash": g.main_image_hash, "price_fen": g.price_fen, "skus": g.skus, "source": source,
    })
    s.flush()
    # 商品表保存最新状态：补采更早的历史时不能把它改回旧值
    latest = s.scalar(select(func.max(ProductSnapshot.day)).where(ProductSnapshot.goods_id == g.goods_id))
    if latest is not None and latest > day:
        if s.scalar(select(Product.id).where(Product.goods_id == g.goods_id)) is not None:
            return
    _upsert(s, Product, {"goods_id": g.goods_id}, {
        "shop_id": shop.id, "name": g.name, "series": g.series, "title": g.title,
        "price_fen": g.price_fen, "main_image_hash": g.main_image_hash,
    })


def _save_traffic(s: Session, _shop: Shop, day: date, t: T.GoodsTraffic, source: str) -> None:
    _upsert(s, DailyMetric, {"goods_id": t.goods_id, "day": t.day or day}, {
        "visitors": t.visitors, "add_to_cart": t.add_to_cart, "orders": t.orders,
        "gmv_fen": t.gmv_fen, "refunds": t.refunds, "source": source,
    })


def _save_order_stats(s: Session, _shop: Shop, day: date, o: T.OrderStats, source: str) -> None:
    _upsert(s, ShopDailyMetric, {"day": o.day or day}, {
        "orders": o.orders, "gmv_fen": o.gmv_fen, "refunds": o.refunds, "source": source,
    })


_SAVERS = {
    "goods_snapshot": _save_goods,
    "goods_daily_traffic": _save_traffic,
    "order_stats": _save_order_stats,
}
