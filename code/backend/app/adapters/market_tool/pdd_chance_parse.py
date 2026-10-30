"""拼多多商家后台「机会商品」数据解析（纯函数）：输入是页面自己请求回来的 JSON，便于用保存的样本测试。

- 热搜商品（热点运营）：chance-goods/get_hot_search_goods，近 7 天的销量档、曝光档、点击档和相关搜索词
- 缺货商品（消费者正在搜）：chance-goods/home_list（banner_type=1），平台说「搜索量大、商品紧缺」，带 0–100 热度
- 顾客在搜的词：chance-goods/get_filters 里的 mall_chance_words，只有词没有数字

后台只给本店主营类目的数据；数字都是档位（「500+」「5000～10000」），取档位中间值。
"""
from __future__ import annotations

import re
from typing import Any

from app.adapters.market_tool.dianba_parse import related
from app.adapters.types import KeywordStat, RankItem

# 后台不给竞争数据：缺货词按平台说法偏低，热搜词取中间值
SHORTAGE_COMPETITION = 0.3
HOT_COMPETITION = 0.5
# 缺货商品的 0–100 热度折算到和热搜词点击数同一量级（100 ≈ 1 万次点击）
HOT_INDEX_SCALE = 100


class PddChanceError(RuntimeError):
    pass


def _result(resp: dict[str, Any]) -> Any:
    if not resp.get("success"):
        raise PddChanceError(f"拼多多后台返回错误：{resp.get('error_msg') or resp.get('error_code')}")
    return resp["result"]


def band(text: str | None) -> float:
    """档位文字 → 数字：「500+」→500，「10w+」→100000，「5000～10000」「500 ~ 1000」→ 中间值。"""
    nums = [float(n) * (10000 if unit in ("w", "万") else 1)
            for n, unit in re.findall(r"(\d+(?:\.\d+)?)\s*(w|万)?", (text or "").lower())]
    return sum(nums) / len(nums) if nums else 0.0


def _clean(word: str) -> str:
    """去掉顾客搜索词前后的空格和符号（「.羊羔绒马甲」「/工厂」）。"""
    return re.sub(r"^[\s\W_]+|[\s\W_]+$", "", word or "")


def hot_search_goods(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """多页热搜商品合并去重。"""
    seen, out = set(), []
    for resp in pages:
        for g in _result(resp).get("opportunity_goods_info_list") or []:
            if g["chance_id"] not in seen:
                seen.add(g["chance_id"])
                out.append(g)
    return out


def hot_goods(pages: list[dict[str, Any]], category: str = "") -> list[RankItem]:
    """热搜商品按销量档、点击档排序；标题和相关词都不含类目词的去掉，一个都不剩时返回全部。
    sales_30d 填的是近 7 天销量档（后台只给这个）。"""
    rows = sorted(hot_search_goods(pages), key=lambda g: (band(g.get("sales")), band(g.get("click_count"))), reverse=True)
    picked = [g for g in rows if category and (category in g["goods_name"]
                                               or any(related(w, category) for w in g.get("related_search_word") or []))]
    return [RankItem(rank=i, title=g["goods_name"], price_fen=int(float(g.get("reference_price") or 0)),
                     sales_30d=int(band(g.get("sales"))), image_url=g.get("image_url") or "")
            for i, g in enumerate(picked or rows, 1)]


def shortage_goods(resp: dict[str, Any]) -> list[dict[str, Any]]:
    return _result(resp).get("list") or []


def chance_words(resp: dict[str, Any]) -> list[str]:
    for f in _result(resp):
        if f.get("params_key") == "mall_chance_words":
            words = [_clean(o.get("title") or o.get("option_key")) for o in f.get("option_info_list") or []]
            return list(dict.fromkeys(w for w in words if len(w) >= 2))
    return []


def keywords(hot_pages: list[dict[str, Any]], shortage_resp: dict[str, Any], filters_resp: dict[str, Any],
             category: str = "") -> list[KeywordStat]:
    """热搜商品的相关搜索词：热度 = 用到这个词的商品点击档之和；
    缺货商品名和顾客在搜的词：标为 shortage，热度取平台热度折算（没有就用热搜词里的同名热度）。
    趋势要和以前的快照比，这里一律给 0，由读取层补。"""
    heat: dict[str, float] = {}
    for g in hot_search_goods(hot_pages):
        for w in g.get("related_search_word") or []:
            w = _clean(w)
            if w:
                heat[w] = heat.get(w, 0.0) + band(g.get("click_count"))
    out = {w: KeywordStat(keyword=w, heat=h, trend=0.0, competition=HOT_COMPETITION) for w, h in heat.items()}
    short = {_clean(g["keywords"]): float(g.get("hot_index") or 0) * HOT_INDEX_SCALE for g in shortage_goods(shortage_resp)}
    for w in chance_words(filters_resp):
        short.setdefault(w, 0.0)
    for w, h in short.items():
        if w:
            out[w] = KeywordStat(keyword=w, heat=max(h, heat.get(w, 0.0)), trend=0.0, competition=SHORTAGE_COMPETITION,
                                 shortage=True)
    return [k for k in out.values() if related(k.keyword, category)]
