from __future__ import annotations

from datetime import date

from app.adapters import fixtures
from app.adapters.types import GoodsInfo, GoodsPatch, GoodsTraffic, KeywordStat, ListingPackage, OrderStats, PromotionStat, WriteResult


class FakePddShop:
    def list_goods(self, shop_id: str, day: date) -> list[GoodsInfo]:
        return fixtures.goods_list(day)

    def goods_daily_traffic(self, shop_id: str, day: date) -> list[GoodsTraffic]:
        return fixtures.goods_traffic(day)

    def order_stats(self, shop_id: str, day: date) -> OrderStats:
        return fixtures.order_stats(day)

    def promotion_daily(self, shop_id: str, day: date) -> list[PromotionStat]:
        return fixtures.promotion_daily(day)

    def keyword_insight(self, keywords: list[str]) -> list[KeywordStat]:
        return fixtures.keyword_stats(keywords)


class FakePddWriter:
    """记录写入调用，不真正写店铺。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def create_goods_draft(self, pkg: ListingPackage) -> WriteResult:
        self.calls.append(("create_goods_draft", pkg.model_dump()))
        return WriteResult(ok=True, ref=f"draft_{len(self.calls)}")

    def update_goods(self, goods_id: str, patch: GoodsPatch, before: GoodsInfo) -> WriteResult:
        self.calls.append(("update_goods", {"goods_id": goods_id, **patch.model_dump(exclude_none=True)}))
        return WriteResult(ok=True, ref=goods_id)
