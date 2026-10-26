"""选品智能体（只读）：需求侦察 → 货源匹配 → 硬性过滤 → 同款审查 → 打分 → 推荐撰写。

数据采集、过滤、利润、打分都是程序；模型只做同款判断与推荐理由。
"""
from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import capabilities as caps
from app.adapters.types import OfferDetail, OfferFilter
from app.agents.facts import FactTable
from app.agents.runtime import AgentRunner, AgentSpec
from app.config import get_settings
from app.models import Candidate, Demand, Offer
from app.rules import selection as R
from app.services import Services
from app.workflow.state_machine import Actor, transition

SELECTOR = Actor("agent", "selection")
SAME_PRODUCT_MIN_CONF = 0.8


class SameProductOut(BaseModel):
    same_product: bool
    confidence: float
    evidence: list[str]
    concerns: list[str]


class RecommendOut(BaseModel):
    reason: str
    risks: list[str]


SAME = AgentSpec(name="selection.same_product", model_role="light", output=SameProductOut)
RECOMMEND = AgentSpec(name="selection.recommend", model_role="main", output=RecommendOut)


def _sourcing(s: Session, services: Services):
    for ch, cap in (("api", "alibaba_api"), ("web", "alibaba_web")):
        if caps.is_healthy(s, cap) and services.sourcing.get(ch) is not None:
            return services.sourcing[ch]
    return None


def scout_demands(s: Session, services: Services, week: str, category: str) -> list[Demand]:
    """机会选品：热词趋势上升的词作为需求。市场工具不可用时，后续改用拼多多前台网页读取。"""
    if services.market is None or not caps.is_healthy(s, "market_tool"):
        return []
    out = []
    ranking = services.market.category_ranking(category, week)
    for kw in services.market.hot_keywords(category, week):
        if kw.trend <= 0.1:
            continue
        matched = [r for r in ranking if kw.keyword in r.title] or ranking[:1]
        d = Demand(week=week, keyword=kw.keyword, seed_type="opportunity", heat=kw.heat, trend=kw.trend,
                   competitors=[{**r.model_dump(), "competition": kw.competition} for r in matched])
        s.add(d)
        s.flush()
        out.append(d)
    return out


def _upsert_offer(s: Session, d: OfferDetail) -> Offer:
    o = s.get(Offer, d.offer_id)
    if o is None:
        o = Offer(id=d.offer_id, title=d.title, price_fen=d.price_fen)
        s.add(o)
    for f in ("title", "category", "price_fen", "shipping_fen", "dropship", "stock", "ship_48h_rate",
              "repurchase_rate", "return_rate", "supplier_years", "weight_kg", "longest_side_cm", "image_urls"):
        setattr(o, f, getattr(d, f))
    o.raw = d.model_dump()
    s.flush()  # 先落盘货源，候选表的外键才能引用
    return o


def run_weekly(runner: AgentRunner, s: Session, services: Services, week: str, category: str) -> list[Candidate]:
    rules = get_settings().rules["selection"]
    source = _sourcing(s, services)
    if source is None:
        return []
    recommended: list[tuple[float, Candidate]] = []
    for demand in scout_demands(s, services, week, category):
        competitor = demand.competitors[0]
        sell_price = competitor["price_fen"]
        for brief in source.search_by_keyword(demand.keyword, OfferFilter(), limit=20):
            detail = source.offer_detail(brief.offer_id)
            _upsert_offer(s, detail)
            cand = Candidate(demand_id=demand.id, offer_id=detail.offer_id)
            s.add(cand)
            s.flush()
            reasons = R.hard_filter(detail, sell_price, rules["hard_filters"], rules["profit"])
            if reasons:
                cand.reject_reason = "；".join(reasons)
                transition(s, "candidate", cand.id, "drop", SELECTOR)
                continue
            transition(s, "candidate", cand.id, "filter", SELECTOR)

            same = runner.run(SAME, f"竞品：{competitor['title']}\n货源：{detail.title}\n判断是否同款。",
                              context={"competitor": competitor["title"], "offer": detail.title})
            cand.same_conf = same.confidence
            if not same.same_product or same.confidence < SAME_PRODUCT_MIN_CONF:
                cand.reject_reason = "同款置信度不足：" + "；".join(same.concerns)
                transition(s, "candidate", cand.id, "drop", SELECTOR)
                continue
            transition(s, "candidate", cand.id, "verify", SELECTOR)

            p = R.profit(sell_price, detail, rules["profit"])
            auto = services.dropship.check_auto_order(detail.offer_id).supported if services.dropship else None
            total, breakdown = R.score(detail, p, demand.heat, demand.trend, competitor.get("competition", 0.5),
                                       auto, fit=0.9, assets=0.6, weights=rules["score_weights"])
            cand.score, cand.breakdown = total, breakdown
            cand.price_plan = {"sell_price_fen": sell_price, "cost_fen": p.cost_fen, "shipping_fen": p.shipping_fen,
                               "fees_fen": p.fees_fen, "profit_fen": p.profit_fen, "margin": round(p.margin, 3)}
            transition(s, "candidate", cand.id, "score", SELECTOR)
            if total < rules["candidate_min_score"]:
                cand.reject_reason = f"得分 {total} 低于 {rules['candidate_min_score']}"
                transition(s, "candidate", cand.id, "drop", SELECTOR)
                continue
            recommended.append((total, cand))

    recommended.sort(key=lambda x: -x[0])
    out = []
    for total, cand in recommended[: rules["weekly_max_recommend"]]:
        offer = s.get(Offer, cand.offer_id)
        demand = s.get(Demand, cand.demand_id)
        facts = FactTable()
        pp = cand.price_plan
        facts.add("plan.sell_price_yuan", pp["sell_price_fen"] / 100)
        facts.add("plan.cost_yuan", pp["cost_fen"] / 100)
        facts.add("plan.profit_yuan", pp["profit_fen"] / 100)
        facts.add("plan.margin_pct", round(pp["margin"] * 100, 1))
        facts.add("cand.score", total)
        facts.add("demand.trend_pct", round(demand.trend * 100, 1))
        facts.add("supplier.years", offer.supplier_years)
        facts.add("supplier.ship_48h_pct", round(offer.ship_48h_rate * 100, 1))
        facts.ignore_strings.update({offer.title, demand.keyword, "48小时", "48 小时"})
        rec = runner.run(RECOMMEND, "请为这款候选写推荐理由（「建议上新……，因为……」）与风险提示。\n" + facts.as_prompt_lines()
                         + f"\n需求词：{demand.keyword}\n货源：{offer.title}",
                         facts=facts, context={"keyword": demand.keyword, "offer_title": offer.title})
        cand.reason, cand.risks = rec.reason, rec.risks
        transition(s, "candidate", cand.id, "recommend", SELECTOR)
        out.append(cand)
    return out


def recommended_this_week(s: Session, week: str) -> list[Candidate]:
    return list(s.scalars(
        select(Candidate).join(Demand, Demand.id == Candidate.demand_id)
        .where(Demand.week == week, Candidate.status == "recommended").order_by(Candidate.score.desc())
    ).all())
