"""指标计算与异常检测（纯函数，不读数据库、不调模型）。"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Iterable


@dataclass(frozen=True)
class DayRow:
    goods_id: str
    day: date
    visitors: int
    add_to_cart: int
    orders: int
    gmv_fen: int


@dataclass(frozen=True)
class Window:
    visitors: int
    add_to_cart: int
    orders: int
    gmv_fen: int

    @property
    def cvr(self) -> float:
        return self.orders / self.visitors if self.visitors else 0.0

    @property
    def cart_rate(self) -> float:
        return self.add_to_cart / self.visitors if self.visitors else 0.0


def window(rows: Iterable[DayRow], end: date, days: int) -> Window:
    start = end - timedelta(days=days - 1)
    sel = [r for r in rows if start <= r.day <= end]
    return Window(
        visitors=sum(r.visitors for r in sel),
        add_to_cart=sum(r.add_to_cart for r in sel),
        orders=sum(r.orders for r in sel),
        gmv_fen=sum(r.gmv_fen for r in sel),
    )


@dataclass(frozen=True)
class GoodsCompare:
    goods_id: str
    current: Window   # 截至 end 的近 N 天
    previous: Window  # 再往前 N 天

    @property
    def cvr_ratio(self) -> float | None:
        return self.current.cvr / self.previous.cvr if self.previous.cvr else None


def compare(rows: list[DayRow], goods_id: str, end: date, days: int = 7) -> GoodsCompare:
    mine = [r for r in rows if r.goods_id == goods_id]
    return GoodsCompare(goods_id, window(mine, end, days), window(mine, end - timedelta(days=days), days))


def cvr_anomaly(c: GoodsCompare, min_visitors: int, drop_ratio: float) -> bool:
    """转化率异常：两段样本都够，且近 N 天转化率低于前 N 天 × drop_ratio。"""
    if c.current.visitors < min_visitors or c.previous.visitors < min_visitors:
        return False
    ratio = c.cvr_ratio
    return ratio is not None and ratio < drop_ratio


def classify(compares: list[GoodsCompare]) -> dict[str, list[str]]:
    """早报分组：最好卖（订单最多）/ 掉在哪（转化率下滑最明显）/ 正常。"""
    if not compares:
        return {"best": [], "dropping": [], "normal": []}
    best = max(compares, key=lambda c: c.current.orders).goods_id
    dropping = [c.goods_id for c in compares if c.cvr_ratio is not None and c.cvr_ratio < 0.85 and c.goods_id != best]
    normal = [c.goods_id for c in compares if c.goods_id not in dropping and c.goods_id != best]
    return {"best": [best], "dropping": dropping, "normal": normal}
