"""选品规则（04 号文档 3.4–3.5 节）：硬性过滤、利润公式、打分、测款判定。纯函数。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.adapters.types import OfferDetail


def hard_filter(offer: OfferDetail, sell_price_fen: int, cfg: dict[str, Any], profit_cfg: dict[str, Any]) -> list[str]:
    """返回不通过的原因；空列表 = 通过。"""
    reasons = []
    if cfg["require_dropship"] and not offer.dropship:
        reasons.append("不支持一件代发")
    p = profit(sell_price_fen, offer, profit_cfg)
    if p.margin < cfg["min_margin"]:
        reasons.append(f"毛利率 {p.margin:.0%} 低于 {cfg['min_margin']:.0%}")
    if p.profit_fen < cfg["min_profit_per_unit"] * 100:
        reasons.append(f"单件利润 {p.profit_fen / 100:.2f} 元低于 {cfg['min_profit_per_unit']} 元")
    if offer.ship_48h_rate < cfg["min_ship_48h_rate"]:
        reasons.append(f"48 小时发货率 {offer.ship_48h_rate:.0%} 偏低")
    if offer.category_return_rate and offer.return_rate > offer.category_return_rate * cfg["max_return_rate_vs_category"]:
        reasons.append("退货率高于类目均值")
    if any(b in offer.category or b in offer.title for b in cfg["banned_categories"]):
        reasons.append("属于限制或需资质类目")
    if offer.weight_kg > cfg["max_weight_kg"] or offer.longest_side_cm > cfg["max_longest_side_cm"]:
        reasons.append("体积或重量超限")
    if offer.supplier_years < cfg["min_supplier_years"]:
        reasons.append(f"供应商经营 {offer.supplier_years} 年，不足 {cfg['min_supplier_years']} 年")
    if offer.stock < cfg["min_stock"]:
        reasons.append(f"库存 {offer.stock} 不足 {cfg['min_stock']}")
    return reasons


@dataclass(frozen=True)
class Profit:
    sell_price_fen: int
    cost_fen: int
    shipping_fen: int
    fees_fen: int
    profit_fen: int

    @property
    def margin(self) -> float:
        return self.profit_fen / self.sell_price_fen if self.sell_price_fen else 0.0


def profit(sell_price_fen: int, offer: OfferDetail, cfg: dict[str, Any]) -> Profit:
    """利润 = 售价 − 进价 − 运费 − 平台费 − 售后损耗 − 推广 − 工具费。"""
    rate = cfg["platform_fee_rate"] + cfg["promotion_rate"] + cfg["after_sales_loss_rate"]
    fees = round(sell_price_fen * rate + cfg["tool_fee_per_order"] * 100)
    p = sell_price_fen - offer.price_fen - offer.shipping_fen - fees
    return Profit(sell_price_fen, offer.price_fen, offer.shipping_fen, fees, p)


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def score(
    offer: OfferDetail,
    p: Profit,
    keyword_heat: float,
    keyword_trend: float,
    competition: float,
    dropship_auto: bool | None,
    fit: float,
    assets: float,
    weights: dict[str, int],
) -> tuple[float, dict[str, float]]:
    """7 个维度各自归一到 0–1，再乘权重。返回 (总分, 明细)。"""
    parts = {
        "profit": _clamp01((p.margin - 0.15) / 0.25),                     # 15% → 0，40% → 满分
        "demand": _clamp01(keyword_heat / 20000 * 0.6 + _clamp01(keyword_trend / 0.5) * 0.4),
        "competition": _clamp01(1 - competition),
        "supplier": _clamp01((offer.supplier_years / 5) * 0.4 + offer.ship_48h_rate * 0.3 + offer.repurchase_rate / 0.4 * 0.3),
        "dropship_auto": 1.0 if dropship_auto else 0.0,                    # 未知按 0 计
        "fit": _clamp01(fit),
        "assets": _clamp01(assets),
    }
    breakdown = {k: round(v * weights[k], 1) for k, v in parts.items()}
    return round(sum(breakdown.values()), 1), breakdown


def trial_verdict(visitors: int, cvr: float, shop_cvr: float, cfg: dict[str, Any]) -> str:
    """14 天测款判定：keep / drop / optimize。"""
    if visitors < cfg["drop_max_visitors"] or (shop_cvr and cvr < shop_cvr * cfg["drop_max_cvr_ratio"]):
        return "drop"
    if visitors >= cfg["keep_min_visitors"] and shop_cvr and cvr >= shop_cvr * cfg["keep_min_cvr_ratio"]:
        return "keep"
    return "optimize"
