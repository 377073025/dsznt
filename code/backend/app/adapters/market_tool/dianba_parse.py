"""电霸数据解析（纯函数）：输入是电霸页面自己请求回来的 JSON，便于用保存的样本测试。

热词：search/hotWordList（pv 搜索人气、trend 涨跌、compete_value 竞争值）
热销产品：industry/hotGoods/getHotGoodsData（月销、周销、日销）
"""
from __future__ import annotations

from datetime import date
from typing import Any

from app.adapters.types import KeywordStat, RankItem


class DianbaDataError(RuntimeError):
    pass


def _body(resp: dict[str, Any]) -> Any:
    if resp.get("error_code") != 0:
        raise DianbaDataError(f"电霸返回错误：{resp.get('message') or resp.get('error_code')}")
    return resp["data"]


def related(word: str, category: str) -> bool:
    """词和类目互相包含即算相关（「香薰」⊂「香薰蜡烛」，「香薰蜡烛礼盒」⊃「香薰蜡烛」）；没给类目时全要。"""
    return not category or category in word or word in category


def hot_words(resp: dict[str, Any], category: str = "") -> tuple[list[KeywordStat], str]:
    """返回 (热词, 数据日期)。竞争度用竞争值在全部热词里的百分位换算到 0–1。"""
    rows = _body(resp)["list"]
    values = sorted(r["compete_value"] for r in rows)
    pct = {v: (values.index(v) + 1) / len(values) for v in values}
    out = [KeywordStat(keyword=r["word"], heat=float(r["pv"]), trend=float(r["trend"]),
                       competition=round(pct[r["compete_value"]], 3))
           for r in rows if related(r["word"], category)]
    return out, max((r["date"] for r in rows), default="")


def hot_goods(resp: dict[str, Any], category: str = "") -> tuple[list[RankItem], str]:
    """返回 (按月销排序的热销商品, 数据日期)；类目词不在标题里的去掉，一个都不剩时返回全部。"""
    rows = sorted(_body(resp), key=lambda r: r.get("month_sales") or 0, reverse=True)
    picked = [r for r in rows if category and category in r["goods_name"]] or rows
    items = [RankItem(rank=i, title=r["goods_name"], price_fen=round(float(r["price"]) * 100),
                      sales_30d=int(r.get("month_sales") or 0), image_url=r.get("thumb_url") or "")
             for i, r in enumerate(picked, 1)]
    return items, max((r.get("show_time") or "" for r in rows), default="")


def check_fresh(data_date: str, today: date, max_age_days: int, what: str) -> None:
    """数据太旧（例如试用账号只给历史样本）就不用，免得拿过期热度去选品。"""
    if not data_date:
        raise DianbaDataError(f"电霸{what}没有数据日期")
    age = (today - date.fromisoformat(data_date[:10])).days
    if age > max_age_days:
        raise DianbaDataError(f"电霸{what}数据日期是 {data_date[:10]}，已过 {age} 天（超过 {max_age_days} 天不用）")
