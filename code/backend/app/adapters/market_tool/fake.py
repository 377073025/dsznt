from __future__ import annotations

from app.adapters import fixtures
from app.adapters.types import CompetitorGoods, KeywordStat, RankItem


class FakeMarketData:
    def hot_keywords(self, category: str, week: str) -> list[KeywordStat]:
        return fixtures.keyword_stats(["葡萄柚香薰蜡烛", "柑橘香薰", "香薰蜡烛礼盒"])

    def category_ranking(self, category: str, week: str) -> list[RankItem]:
        return fixtures.category_ranking(category)

    def competitor_goods(self, goods_url: str) -> CompetitorGoods:
        return fixtures.competitor(goods_url)
