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


class ComplianceOut(BaseModel):
    passed: bool
    risk_level: str      # low / medium / high
    issues: list[str]    # 具体问题，例如「标题含品牌词」「图片疑似盗用品牌官方图」


SAME = AgentSpec(name="selection.same_product", model_role="light", output=SameProductOut)
COMPLIANCE = AgentSpec(name="selection.compliance", model_role="main", output=ComplianceOut)
RECOMMEND = AgentSpec(name="selection.recommend", model_role="main", output=RecommendOut)


def sourcing(s: Session, services: Services):
    for ch, cap in (("api", "alibaba_api"), ("web", "alibaba_web")):
        if caps.is_healthy(s, cap) and services.sourcing.get(ch) is not None:
            return services.sourcing[ch]
    return None


class DemandDraft(BaseModel):
    keyword: str
    heat: float
    trend: float
    competition: float
    competitors: list[dict]


MIN_TREND = 0.1


def find_demands(s: Session, services: Services, week: str, category: str, keyword: str = "") -> list[DemandDraft]:
    """能力「选品 · 需求侦察」（只读）：热词趋势上升的词作为需求；给了 keyword 时只看包含它的词、不卡趋势门槛。"""
    if services.market is None or not caps.is_healthy(s, "market_tool"):
        return []
    ranking = services.market.category_ranking(category, week)
    out = []
    for kw in services.market.hot_keywords(category, week):
        if (keyword and keyword not in kw.keyword) or (not keyword and kw.trend <= MIN_TREND):
            continue
        matched = [r for r in ranking if kw.keyword in r.title] or ranking[:1]
        out.append(DemandDraft(keyword=kw.keyword, heat=kw.heat, trend=kw.trend, competition=kw.competition,
                               competitors=[{**r.model_dump(), "competition": kw.competition} for r in matched]))
    return out


def find_extensions(s: Session, services: Services, week: str, category: str) -> list[tuple[str, DemandDraft]]:
    """延伸选品（04 号文档 3.2 节）：店内最好卖的款 → 同系列、不和本店商品重复的热词（不卡趋势门槛）。

    返回 (好卖款 goods_id, 需求)。同供应商的关联款需要 1688 店铺商品接口，接入 alibaba_api 后补充。
    """
    from app.data import queries
    from app.rules.metrics import classify, compare

    if services.market is None or not caps.is_healthy(s, "market_tool"):
        return []
    day = queries.business_day(s)
    rows = queries.day_rows(s, day, 14)
    products = {p.goods_id: p for p in queries.products(s)}
    if not rows or not products:
        return []
    best = classify([compare(rows, g, day) for g in products])["best"]
    own = [p.name for p in products.values()]
    ranking = services.market.category_ranking(category, week)
    out, seen = [], set()
    for gid in best:
        series = products[gid].series or category
        for kw in services.market.hot_keywords(category, week):
            if kw.keyword in seen or series not in kw.keyword or kw.trend < 0 \
                    or any(kw.keyword in n or n in kw.keyword for n in own):
                continue
            seen.add(kw.keyword)
            matched = [r for r in ranking if kw.keyword in r.title] or ranking[:1]
            out.append((gid, DemandDraft(keyword=kw.keyword, heat=kw.heat, trend=kw.trend, competition=kw.competition,
                                         competitors=[{**r.model_dump(), "competition": kw.competition} for r in matched])))
    return out


def scout_demands(s: Session, services: Services, week: str, category: str) -> list[Demand]:
    """需求清单：延伸选品（店内好卖款的同系列）+ 机会选品（热度上升的词）；同一个词只算一次，延伸优先。
    市场工具不可用时，后续改用拼多多前台网页读取。"""
    drafts: dict[str, tuple[str, str, DemandDraft]] = {}
    for gid, d in find_extensions(s, services, week, category):
        drafts[d.keyword] = ("extension", gid, d)
    for d in find_demands(s, services, week, category):
        drafts.setdefault(d.keyword, ("opportunity", "", d))
    out = []
    for seed_type, seed_ref, d in drafts.values():
        demand = Demand(week=week, keyword=d.keyword, seed_type=seed_type, seed_ref=seed_ref, heat=d.heat, trend=d.trend,
                        competitors=d.competitors)
        s.add(demand)
        s.flush()
        out.append(demand)
    return out


def check_same(runner: AgentRunner, competitor_title: str, offer_title: str) -> SameProductOut:
    return runner.run(SAME, f"竞品：{competitor_title}\n货源：{offer_title}\n判断是否同款。",
                      context={"competitor": competitor_title, "offer": offer_title})


def check_compliance(runner: AgentRunner, detail: OfferDetail) -> ComplianceOut:
    """商品风险（品牌 / IP 侵权、盗图、描述夸大）：品牌词库命中直接不通过，不调模型；否则交模型看标题与图片。"""
    cfg = get_settings().rules["selection"]["compliance"]
    text = f"{detail.title} {detail.category}".lower()
    hits = [w for w in cfg["brand_words"] if w.lower() in text]
    if hits:
        return ComplianceOut(passed=False, risk_level="high", issues=[f"标题含品牌 / IP 词：{'、'.join(hits)}"])
    if not cfg.get("llm_review", True):
        return ComplianceOut(passed=True, risk_level="low", issues=[])
    return runner.run(COMPLIANCE, (
        f"请审查这个 1688 货源能否在拼多多上架（只看商品风险：品牌或 IP 侵权、外观专利、盗用他人图片、描述夸大）。\n"
        f"标题：{detail.title}\n类目：{detail.category}\n图片：{'、'.join(detail.image_urls) or '无'}"
    ), context={"title": detail.title, "category": detail.category, "images": detail.image_urls})


def price_plan(sell_price_fen: int, detail: OfferDetail) -> dict:
    p = R.profit(sell_price_fen, detail, get_settings().rules["selection"]["profit"])
    return {"sell_price_fen": sell_price_fen, "cost_fen": p.cost_fen, "shipping_fen": p.shipping_fen,
            "fees_fen": p.fees_fen, "profit_fen": p.profit_fen, "margin": round(p.margin, 3)}


def score_offer(services: Services, detail: OfferDetail, sell_price_fen: int, heat: float, trend: float,
                competition: float, fit: float = 0.9) -> tuple[float, dict]:
    """fit：与店铺契合度，延伸选品（店内好卖款的同系列）给满分。"""
    rules = get_settings().rules["selection"]
    p = R.profit(sell_price_fen, detail, rules["profit"])
    auto = services.dropship.check_auto_order(detail.offer_id).supported if services.dropship else None
    return R.score(detail, p, heat, trend, competition, auto, fit=fit, assets=0.6, weights=rules["score_weights"])


def write_recommendation(runner: AgentRunner, offer_title: str, supplier_years: int, ship_48h_rate: float,
                         keyword: str, trend: float, plan: dict, total: float) -> RecommendOut:
    """推荐理由：数字全部由程序给出，模型只组织语言。"""
    facts = FactTable()
    facts.add("plan.sell_price_yuan", plan["sell_price_fen"] / 100)
    facts.add("plan.cost_yuan", plan["cost_fen"] / 100)
    facts.add("plan.profit_yuan", plan["profit_fen"] / 100)
    facts.add("plan.margin_pct", round(plan["margin"] * 100, 1))
    facts.add("cand.score", total)
    facts.add("demand.trend_pct", round(trend * 100, 1))
    facts.add("supplier.years", supplier_years)
    facts.add("supplier.ship_48h_pct", round(ship_48h_rate * 100, 1))
    facts.ignore_strings.update({offer_title, keyword, "48小时", "48 小时"})
    return runner.run(RECOMMEND, "请为这款候选写推荐理由（「建议上新……，因为……」）与风险提示。\n" + facts.as_prompt_lines()
                      + f"\n需求词：{keyword}\n货源：{offer_title}",
                      facts=facts, context={"keyword": keyword, "offer_title": offer_title})


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
    source = sourcing(s, services)
    if source is None:
        return []
    recommended: list[tuple[float, Candidate]] = []
    verified: dict[int, list[tuple[float, str]]] = {}  # 需求 → 通过过滤与同款审查的货源，作推荐款的备选货源
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

            same = check_same(runner, competitor["title"], detail.title)
            cand.same_conf = same.confidence
            if not same.same_product or same.confidence < SAME_PRODUCT_MIN_CONF:
                cand.reject_reason = "同款置信度不足：" + "；".join(same.concerns)
                transition(s, "candidate", cand.id, "drop", SELECTOR)
                continue
            comp = check_compliance(runner, detail)
            cand.compliance = comp.model_dump()
            if not comp.passed:
                cand.reject_reason = "合规审查未通过：" + "；".join(comp.issues)
                transition(s, "candidate", cand.id, "drop", SELECTOR)
                continue
            transition(s, "candidate", cand.id, "verify", SELECTOR)

            total, breakdown = score_offer(services, detail, sell_price, demand.heat, demand.trend,
                                           competitor.get("competition", 0.5),
                                           fit=1.0 if demand.seed_type == "extension" else 0.9)
            cand.score, cand.breakdown = total, breakdown
            cand.price_plan = price_plan(sell_price, detail)
            verified.setdefault(demand.id, []).append((total, detail.offer_id))
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
        rec = write_recommendation(runner, offer.title, offer.supplier_years, offer.ship_48h_rate,
                                   demand.keyword, demand.trend, cand.price_plan, total)
        cand.reason, cand.risks = rec.reason, [*rec.risks, *cand.compliance.get("issues", [])]
        cand.backup_offer_ids = [o for _, o in sorted(verified.get(cand.demand_id, []), reverse=True) if o != cand.offer_id]
        transition(s, "candidate", cand.id, "recommend", SELECTOR)
        out.append(cand)
    return out


# ---------- 改一改 ----------

class CandidateRevision(BaseModel):
    sell_price_yuan: float  # 店主要求的新售价（元）；0 表示不改售价
    switch_offer: bool      # 是否换成备选货源
    note: str               # 一句话说明怎么理解店主的意见


REVISE = AgentSpec(name="selection.revise", model_role="main", output=CandidateRevision)


def revise_candidate(runner: AgentRunner, s: Session, services: Services, cand_id: int, feedback: str) -> Candidate:
    """候选「改一改」：模型只负责理解意见（改售价 / 换货源）；利润、打分、过滤由程序重算，推荐理由重写。改完回到「已推荐」等拍板。"""
    cand = s.get(Candidate, cand_id)
    assert cand is not None and cand.status == "revising"
    demand = s.get(Demand, cand.demand_id)
    sell = cand.price_plan.get("sell_price_fen", 0)
    out = runner.run(REVISE, (
        f"店主对这款选品候选提出了修改意见，请判断要改什么。\n候选：{s.get(Offer, cand.offer_id).title}；当前售价 {sell / 100:.2f} 元；"
        f"备选货源 {len(cand.backup_offer_ids)} 个\n店主意见：{feedback}\n"
        "只能改两件事：售价（sell_price_yuan，不改填 0）、换成备选货源（switch_offer）。其余意见写在 note 里。"
    ), context={"feedback": feedback, "sell_price_yuan": sell / 100, "backups": cand.backup_offer_ids})

    notes = []
    if out.sell_price_yuan > 0:
        sell = round(out.sell_price_yuan * 100)
    if out.switch_offer:
        if cand.backup_offer_ids:
            new_id, *rest = cand.backup_offer_ids
            cand.backup_offer_ids, cand.offer_id = [*rest, cand.offer_id], new_id
        else:
            notes.append("没有可换的备选货源，仍用原货源")
    offer = s.get(Offer, cand.offer_id)
    detail = OfferDetail.model_validate(offer.raw)
    comp = demand.competitors[0] if demand.competitors else {}
    cand.price_plan = price_plan(sell, detail)
    cand.score, cand.breakdown = score_offer(services, detail, sell, demand.heat, demand.trend,
                                             comp.get("competition", 0.5), fit=1.0 if demand.seed_type == "extension" else 0.9)
    rules = get_settings().rules["selection"]
    reasons = R.hard_filter(detail, sell, rules["hard_filters"], rules["profit"])
    rec = write_recommendation(runner, offer.title, offer.supplier_years, offer.ship_48h_rate, demand.keyword,
                               demand.trend, cand.price_plan, cand.score)
    cand.reason = rec.reason
    cand.risks = [*(f"未通过硬性过滤：{r}" for r in reasons), *notes, *rec.risks]
    cand.revise_rounds += 1
    transition(s, "candidate", cand.id, "recommend", SELECTOR, {"feedback": feedback, "note": out.note})
    return cand


def recommended_this_week(s: Session, week: str) -> list[Candidate]:
    return list(s.scalars(
        select(Candidate).join(Demand, Demand.id == Candidate.demand_id)
        .where(Demand.week == week, Candidate.status == "recommended").order_by(Candidate.score.desc())
    ).all())
