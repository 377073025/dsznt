"""模拟数据：视频示例店铺（柑橘、海盐、薰衣草、雪松），含「海盐换白底图」事件。

数字按日期确定性生成，任何一天都能取到数据；
海盐主图在 HAIYAN_IMAGE_CHANGED_ON 当天换成白底图，之后转化率明显下降。
"""
from __future__ import annotations

from datetime import date

from app.adapters.types import (
    CompetitorGoods,
    GoodsInfo,
    GoodsTraffic,
    KeywordStat,
    OfferBrief,
    OfferDetail,
    OrderStats,
    PromotionStat,
    RankItem,
)

HAIYAN_IMAGE_CHANGED_ON = date(2026, 10, 17)

# goods_id, 名称, 系列, 售价(分), 日访客基数, 转化率
_GOODS = [
    ("g_citrus", "柑橘香薰蜡烛", "香薰蜡烛", 2990, 420, 0.052),
    ("g_haiyan", "海盐香薰蜡烛", "香薰蜡烛", 2990, 360, 0.041),
    ("g_lavender", "薰衣草香薰蜡烛", "香薰蜡烛", 2990, 300, 0.038),
    ("g_cedar", "雪松香薰蜡烛", "香薰蜡烛", 3290, 150, 0.030),
]


def _wiggle(goods_id: str, day: date) -> float:
    """±8% 的确定性波动，避免数据过于平滑。"""
    seed = (sum(map(ord, goods_id)) * 31 + day.toordinal() * 17) % 17
    return 0.92 + seed / 100


def goods_list(day: date) -> list[GoodsInfo]:
    out = []
    for gid, name, series, price, _, _ in _GOODS:
        image = "img_white_bg_v2" if gid == "g_haiyan" and day >= HAIYAN_IMAGE_CHANGED_ON else f"img_{gid}_scene_v1"
        out.append(
            GoodsInfo(
                goods_id=gid,
                name=name,
                series=series,
                title=f"{name} 200g 家用卧室持久留香 无烟大豆蜡",
                price_fen=price,
                main_image_hash=image,
                skus=[{"name": "200g", "price_fen": price}],
            )
        )
    return out


def goods_traffic(day: date) -> list[GoodsTraffic]:
    out = []
    for gid, _, _, price, base_visitors, cvr in _GOODS:
        if gid == "g_haiyan" and day >= HAIYAN_IMAGE_CHANGED_ON:
            cvr = 0.022
        w = _wiggle(gid, day)
        visitors = round(base_visitors * w)
        orders = round(visitors * cvr)
        out.append(
            GoodsTraffic(
                goods_id=gid,
                day=day,
                visitors=visitors,
                add_to_cart=round(orders * 2.6),
                orders=orders,
                gmv_fen=orders * price,
                refunds=orders // 25,
            )
        )
    return out


def order_stats(day: date) -> OrderStats:
    rows = goods_traffic(day)
    return OrderStats(
        day=day,
        orders=sum(r.orders for r in rows),
        gmv_fen=sum(r.gmv_fen for r in rows),
        refunds=sum(r.refunds for r in rows),
    )


def promotion_daily(day: date) -> list[PromotionStat]:
    return [
        PromotionStat(goods_id=gid, day=day, spend_fen=3000, impressions=base * 8, clicks=base // 3)
        for gid, _, _, _, base, _ in _GOODS
    ]


def keyword_stats(keywords: list[str]) -> list[KeywordStat]:
    table = {
        "葡萄柚香薰蜡烛": KeywordStat(keyword="葡萄柚香薰蜡烛", heat=8200, trend=0.34, competition=0.42),
        "柑橘香薰": KeywordStat(keyword="柑橘香薰", heat=15600, trend=0.12, competition=0.66),
        "香薰蜡烛礼盒": KeywordStat(keyword="香薰蜡烛礼盒", heat=21000, trend=0.05, competition=0.81),
    }
    return [table.get(k, KeywordStat(keyword=k, heat=1000, trend=0.0, competition=0.5)) for k in keywords]


def category_ranking(category: str) -> list[RankItem]:
    return [
        RankItem(rank=1, title="香薰蜡烛礼盒 3 只装", price_fen=5990, sales_30d=12000),
        RankItem(rank=2, title="葡萄柚香薰蜡烛 200g", price_fen=2990, sales_30d=5400),
        RankItem(rank=3, title="无火香薰 藤条 150ml", price_fen=1990, sales_30d=9800),
    ]


def competitor(url: str) -> CompetitorGoods:
    return CompetitorGoods(url=url, title="葡萄柚香薰蜡烛 200g", price_fen=2990, sales_30d=5400)


_OFFERS = {
    "1688_6543210": OfferDetail(
        offer_id="1688_6543210", title="葡萄柚大豆蜡香薰蜡烛 200g 一件代发", category="香薰蜡烛",
        price_fen=1180, shipping_fen=350, dropship=True, stock=3200, ship_48h_rate=0.98,
        repurchase_rate=0.32, return_rate=0.02, category_return_rate=0.035, supplier_years=5,
        weight_kg=0.45, longest_side_cm=10, image_urls=["offer_6543210_1.jpg"],
    ),
    "1688_6543299": OfferDetail(
        offer_id="1688_6543299", title="葡萄柚香薰蜡烛 玻璃杯 200g", category="香薰蜡烛",
        price_fen=1350, shipping_fen=350, dropship=True, stock=900, ship_48h_rate=0.96,
        repurchase_rate=0.25, return_rate=0.03, category_return_rate=0.035, supplier_years=3,
        weight_kg=0.5, longest_side_cm=10, image_urls=["offer_6543299_1.jpg"],
    ),
    "1688_7000001": OfferDetail(
        offer_id="1688_7000001", title="香薰蜡烛 低价走量 不支持代发", category="香薰蜡烛",
        price_fen=600, shipping_fen=350, dropship=False, stock=10000, ship_48h_rate=0.90,
        repurchase_rate=0.10, return_rate=0.06, category_return_rate=0.035, supplier_years=1,
        weight_kg=0.4, longest_side_cm=9, image_urls=["offer_7000001_1.jpg"],
    ),
}


def offer_search(keyword: str) -> list[OfferBrief]:
    return [OfferBrief(offer_id=o.offer_id, title=o.title, price_fen=o.price_fen) for o in _OFFERS.values()]


def offer_detail(offer_id: str) -> OfferDetail:
    return _OFFERS[offer_id]
