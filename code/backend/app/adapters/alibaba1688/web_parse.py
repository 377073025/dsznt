"""1688 页面解析（纯函数，便于用保存下来的真实页面测试）。

输入是页面正文（innerText）和 HTML；页面结构变了时，这里的正则最先失效，测试会报出来。
"""
from __future__ import annotations

import re
from typing import Any

from app.adapters.types import OfferBrief, OfferDetail

_OFFER_LINK = re.compile(r"detail\.1688\.com/offer/(\d+)\.html")
_IMG = re.compile(r"(https?:)?//cbu01\.alicdn\.com/img/ibank/[\w\-!./]+?\.(?:jpg|jpeg|png|webp)")  # 取到第一个扩展名，去掉 _b.jpg 之类的尺寸后缀


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _yuan_to_fen(s: str) -> int:
    return round(_num(s) * 100)


def _pct(m: re.Match | None, default: float = 0.0) -> float:
    return _num(m.group(1)) / 100 if m else default


def _fix_price_breaks(text: str) -> str:
    """页面把价格拆成「¥\\n0\\n.90」几行，先拼回「¥0.90」。"""
    return re.sub(r"[¥￥]\s*\n?\s*(\d+)\s*\n?\s*(\.\d+)?", lambda m: f"¥{m.group(1)}{m.group(2) or ''}", text)


def parse_search(links: list[tuple[str, str]], limit: int) -> list[OfferBrief]:
    """搜索结果：链接文字里有标题和价格（例如「丑橘香薰蜡烛… ¥ 0 .9」）。

    链接文字有时把价格截断（「¥0.17」只剩「¥0.1」），这里的价格只作排序参考，成本以详情页为准。"""
    out: dict[str, OfferBrief] = {}
    for text, href in links:
        m = _OFFER_LINK.search(href)
        if not m or m.group(1) in out:
            continue
        flat = " ".join(text.split())
        price = re.search(r"[¥￥]\s*(\d+)\s*(\.\s*\d+)?", flat)
        if not price:
            continue
        title = flat[: price.start()].split(" ")[0].strip()
        out[m.group(1)] = OfferBrief(offer_id=f"1688_{m.group(1)}", title=title,
                                     price_fen=_yuan_to_fen(price.group(1) + (price.group(2) or "").replace(" ", "")))
        if len(out) >= limit:
            break
    return list(out.values())


def parse_detail(offer_id: str, title: str, text: str, html: str) -> OfferDetail:
    t = _fix_price_breaks(text)
    sku_prices = [_yuan_to_fen(p) for p in re.findall(r"¥(\d+(?:\.\d+)?)\s*\n\s*库存\s*\d+", t)]
    stock = sum(int(x) for x in re.findall(r"库存\s*(\d+)\s*个", t))
    one_piece = re.search(r"1件包邮\s*\n?\s*[¥￥]?\s*\n?\s*(\d+(?:\.\d+)?)", t)
    if one_piece:  # 一件代发包邮价就是我们的进货成本，运费已含
        price_fen, shipping_fen = _yuan_to_fen(one_piece.group(1)), 0
    else:
        price_fen = min(sku_prices) if sku_prices else _first_price(t)
        ship = re.search(r"运费\s*[¥￥]\s*(\d+(?:\.\d+)?)", t)
        shipping_fen = _yuan_to_fen(ship.group(1)) if ship else 0
    weights, sides = _package(t)
    detail: dict[str, Any] = dict(
        offer_id=offer_id,
        title=re.sub(r"\s*-\s*阿里巴巴\s*$", "", title).strip(),
        category=_param(t, r"\S*种类|类目|品类"),
        price_fen=price_fen,
        shipping_fen=shipping_fen,
        dropship=bool(re.search(r"一件代发|1件包邮|代发下单|商家代发", t)),
        stock=stock,
        # 优先代发数据里的 48 小时揽收率；没有时用店铺的综合履约率
        ship_48h_rate=(_pct(re.search(r"48h揽收率\s*([\d.]+)%", t)) or _pct(re.search(r"48小时揽收率\s*([\d.]+)%", t))
                       or _pct(re.search(r"综合履约率\s*\n?\s*([\d.]+)%", t))),
        repurchase_rate=_pct(re.search(r"店铺回头率\s*\n?\s*([\d.]+)%", t)),
        return_rate=0.0,  # 详情页没有退货率
        supplier_years=int(m.group(1)) if (m := re.search(r"入驻\s*(\d+)\s*年", t)) else 0,
        weight_kg=max(weights, default=0.0) / 1000,
        longest_side_cm=max(sides, default=0.0),
        image_urls=_images(html),
    )
    return OfferDetail(**detail)


def _first_price(t: str) -> int:
    m = re.search(r"¥(\d+(?:\.\d+)?)", t)
    if not m:
        raise ValueError("详情页里找不到价格")
    return _yuan_to_fen(m.group(1))


def _param(t: str, key_pattern: str) -> str:
    m = re.search(rf"\n(?:{key_pattern})\s*\n+\s*([^\n]+)", t)
    return m.group(1).strip() if m else ""


def _package(t: str) -> tuple[list[float], list[float]]:
    """包装信息表：每行「规格 长 宽 高 体积 重量(g)」。"""
    weights, sides = [], []
    block = t.split("商品件重尺", 1)[1].split("商品详情", 1)[0] if "商品件重尺" in t else ""
    for line in block.splitlines():
        nums = re.findall(r"(?<![\w.])(\d+(?:\.\d+)?)(?![\w.])", line)
        if len(nums) >= 5:
            l, w, h, _vol, g = (_num(x) for x in nums[-5:])
            sides.append(max(l, w, h))
            weights.append(g)
    return weights, sides


def _images(html: str) -> list[str]:
    """主图在页面里最先出现；「!!」后面是卖家编号，只留和第一张同一卖家的（去掉同款推荐里别家的图）。"""
    seen: list[str] = []
    for m in _IMG.finditer(html):
        url = m.group(0)
        url = ("https:" + url) if url.startswith("//") else url
        if url not in seen:
            seen.append(url)
    seller = re.search(r"!!(\d+)-", seen[0]) if seen else None
    if seller:
        seen = [u for u in seen if f"!!{seller.group(1)}-" in u]
    return seen[:8]
