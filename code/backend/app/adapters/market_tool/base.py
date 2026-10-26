from __future__ import annotations

from typing import Protocol

from app.adapters.types import CompetitorGoods, KeywordStat, RankItem


class MarketDataAdapter(Protocol):
    def hot_keywords(self, category: str, week: str) -> list[KeywordStat]: ...
    def category_ranking(self, category: str, week: str) -> list[RankItem]: ...
    def competitor_goods(self, goods_url: str) -> CompetitorGoods: ...
