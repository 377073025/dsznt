"""智能体能力目录（07 号文档 17.2 节）：每项能力「输入 → 输出」，主流程与单独任务共用同一份代码。

约定：
- run 只读业务表、不写主流程业务表（不建建议、不发早报、不派活）；结果由调用方决定去向。
- params 是人工提交时的参数，Web 表单由它的 JSON Schema 生成。
- needs：需要的能力开关，每组里任意一个 healthy 即可；uses_web：会用到的网页读取平台（单独任务据此避开采集时段）。
- promote_to：单独任务的结果可以转成什么，转了之后完全走主流程的拍板与写入确认。

（文件名不叫 capabilities.py，是为了和「能力开关」app/capabilities.py 区分。）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.types import ListingPackage, OfferDetail, OfferFilter
from app.agents import ceo, executors, ops, review, selection
from app.agents.runtime import AgentRunner
from app.config import get_settings
from app.data import queries
from app.db import utcnow
from app.models import Product
from app.rules import selection as R
from app.services import Services

AGENT_TITLES = {"selection": "选品", "ops": "运营", "ceo": "CEO", "exec": "执行", "web": "网页认路"}


@dataclass
class CapContext:
    session: Session
    services: Services
    runner: AgentRunner


@dataclass(frozen=True)
class AgentCapability:
    key: str
    agent: str
    title: str
    description: str
    params: type[BaseModel]
    run: Callable[[CapContext, Any], BaseModel]
    needs: tuple[tuple[str, ...], ...] = ()
    uses_web: frozenset[str] = field(default_factory=frozenset)
    adhoc: bool = True
    promote_to: Literal["suggestion", "candidate", None] = None


CATALOG: dict[str, AgentCapability] = {}


def capability(key: str, title: str, description: str, params: type[BaseModel], *,
               needs: tuple[tuple[str, ...], ...] = (), uses_web: set[str] | None = None,
               promote_to: Literal["suggestion", "candidate", None] = None):
    def deco(fn: Callable[[CapContext, Any], BaseModel]) -> Callable[[CapContext, Any], BaseModel]:
        if key in CATALOG:
            raise ValueError(f"duplicate capability {key}")
        CATALOG[key] = AgentCapability(key, key.split(".", 1)[0], title, description, params, fn,
                                       needs, frozenset(uses_web or ()), True, promote_to)
        return fn

    return deco


_LLM = ("llm",)
_SOURCING = ("alibaba_api", "alibaba_web")


def _this_week() -> str:
    iso = (utcnow() + timedelta(hours=8)).date().isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _offer_id(raw: str) -> str:
    """接受 1688 链接或 offer id。"""
    m = re.search(r"(\d{6,})", raw)
    return f"1688_{m.group(1)}" if m and not raw.startswith("1688_") else raw.strip()


def _source(ctx: CapContext):
    src = selection.sourcing(ctx.session, ctx.services)
    if src is None:
        raise RuntimeError("1688 货源通道不可用（alibaba_api / alibaba_web 都未就绪）")
    return src


# ---------- 选品 ----------

class ScoutParams(BaseModel):
    category: str = Field(description="类目，例如 香薰蜡烛")
    keyword: str = Field("", description="只看包含该词的需求（可选）")
    week: str = Field("", description="周次，例如 2026-W43；留空为本周")


class ScoutOut(BaseModel):
    week: str
    demands: list[selection.DemandDraft]


@capability("selection.scout", "需求侦察", "按类目或关键词找热度上升的需求，附竞品。", ScoutParams,
            needs=(("market_tool",),), uses_web={"pdd_front"})
def scout(ctx: CapContext, p: ScoutParams) -> ScoutOut:
    week = p.week or _this_week()
    return ScoutOut(week=week, demands=selection.find_demands(ctx.session, ctx.services, week, p.category, p.keyword))


class FindSourceParams(BaseModel):
    keyword: str = Field(description="要找货源的商品，例如 葡萄柚香薰蜡烛")
    competitor_url: str = Field("", description="拼多多竞品链接（可选，用来取售价和同款比对）")
    sell_price_fen: int = Field(0, description="目标售价（分）；没有竞品链接时必填")
    limit: int = Field(10, ge=1, le=20)


class OfferVerdict(BaseModel):
    offer_id: str
    title: str
    price_fen: int
    passed: bool
    reject_reasons: list[str]
    same_product: bool | None = None
    same_confidence: float = 0.0
    concerns: list[str] = []


class FindSourceOut(BaseModel):
    competitor_title: str
    sell_price_fen: int
    offers: list[OfferVerdict]


@capability("selection.find_source", "找货源", "按商品找 1688 货源，给出硬性过滤与同款结论。", FindSourceParams,
            needs=(_LLM, _SOURCING), uses_web={"alibaba"})
def find_source(ctx: CapContext, p: FindSourceParams) -> FindSourceOut:
    rules = get_settings().rules["selection"]
    title, price = p.keyword, p.sell_price_fen
    if p.competitor_url and ctx.services.market is not None:
        comp = ctx.services.market.competitor_goods(p.competitor_url)
        title, price = comp.title, price or comp.price_fen
    if price <= 0:
        raise ValueError("请给出目标售价或竞品链接")
    src = _source(ctx)
    out = []
    for brief in src.search_by_keyword(p.keyword, OfferFilter(), limit=p.limit):
        d = src.offer_detail(brief.offer_id)
        reasons = R.hard_filter(d, price, rules["hard_filters"], rules["profit"])
        v = OfferVerdict(offer_id=d.offer_id, title=d.title, price_fen=d.price_fen, passed=not reasons, reject_reasons=reasons)
        if not reasons:
            same = selection.check_same(ctx.runner, title, d.title)
            v.same_product, v.same_confidence, v.concerns = same.same_product, same.confidence, same.concerns
            v.passed = same.same_product and same.confidence >= selection.SAME_PRODUCT_MIN_CONF
        out.append(v)
    return FindSourceOut(competitor_title=title, sell_price_fen=price, offers=out)


class EvaluateParams(BaseModel):
    offer: str = Field(description="1688 链接或 offer id")
    sell_price_fen: int = Field(gt=0, description="目标售价（分）")
    keyword: str = Field("", description="对应的需求词（可选，用于推荐理由）")
    heat: float = 0.0
    trend: float = 0.0
    competition: float = 0.5


class EvaluateOut(BaseModel):
    offer_id: str
    title: str
    passed: bool
    reject_reasons: list[str]
    price_plan: dict
    score: float = 0.0
    breakdown: dict = {}
    reason: str = ""
    risks: list[str] = []
    keyword: str = ""
    heat: float = 0.0
    trend: float = 0.0
    offer: dict = {}  # 货源详情快照，转为候选时用


@capability("selection.evaluate_offer", "单品评估", "评估一个 1688 货源：硬性过滤、单件净利、打分、推荐理由。",
            EvaluateParams, needs=(_LLM, _SOURCING), uses_web={"alibaba"}, promote_to="candidate")
def evaluate_offer(ctx: CapContext, p: EvaluateParams) -> EvaluateOut:
    rules = get_settings().rules["selection"]
    d = _source(ctx).offer_detail(_offer_id(p.offer))
    plan = selection.price_plan(p.sell_price_fen, d)
    out = EvaluateOut(offer_id=d.offer_id, title=d.title, passed=False, price_plan=plan, keyword=p.keyword or d.title,
                      heat=p.heat, trend=p.trend, offer=d.model_dump(mode="json"),
                      reject_reasons=R.hard_filter(d, p.sell_price_fen, rules["hard_filters"], rules["profit"]))
    if out.reject_reasons:
        return out
    out.score, out.breakdown = selection.score_offer(ctx.services, d, p.sell_price_fen, p.heat, p.trend, p.competition)
    if out.score < rules["candidate_min_score"]:
        out.reject_reasons = [f"得分 {out.score} 低于 {rules['candidate_min_score']}"]
        return out
    rec = selection.write_recommendation(ctx.runner, d.title, d.supplier_years, d.ship_48h_rate, out.keyword,
                                         p.trend, plan, out.score)
    out.passed, out.reason, out.risks = True, rec.reason, rec.risks
    return out


class TrialParams(BaseModel):
    goods_id: str
    start_date: date
    days: int = Field(14, ge=1, le=60)


class TrialOut(BaseModel):
    goods_id: str
    days_observed: int
    visitors: int
    cvr_pct: float
    shop_cvr_pct: float
    verdict: str  # keep / optimize / drop


@capability("selection.trial", "测款评估", "按测款期数据给出留 / 改 / 汰判断（程序计算，不调模型）。", TrialParams)
def trial(ctx: CapContext, p: TrialParams) -> TrialOut:
    cfg = get_settings().rules["selection"]["test_run"]
    observed = min(p.days, (queries.business_day(ctx.session) - p.start_date).days + 1)
    if observed <= 0:
        raise ValueError("测款还没开始，没有数据")
    visitors, cvr, shop_cvr = review.trial_stats(ctx.session, p.goods_id, p.start_date, observed)
    return TrialOut(goods_id=p.goods_id, days_observed=observed, visitors=visitors, cvr_pct=round(cvr * 100, 1),
                    shop_cvr_pct=round(shop_cvr * 100, 1), verdict=R.trial_verdict(visitors, cvr, shop_cvr, cfg))


# ---------- 运营 ----------

class DiagnoseParams(BaseModel):
    goods_id: str = Field("", description="商品 ID；留空看整店")
    day: date | None = Field(None, description="数据日期；留空为最新一天")
    days: int = Field(7, ge=1, le=30, description="对比窗口天数")


@capability("ops.diagnose", "诊断", "看数诊断，给出建议草稿（不进早报）。", DiagnoseParams, needs=(_LLM,),
            promote_to="suggestion")
def diagnose(ctx: CapContext, p: DiagnoseParams) -> ops.OpsDiagnosis:
    return ops.diagnose(ctx.runner, ctx.session, p.day or queries.business_day(ctx.session), p.goods_id, p.days)


class ReviewParams(BaseModel):
    goods_id: str
    since_day: date | None = Field(None, description="从哪天起的改动；留空为近 30 天")


class ReviewItem(BaseModel):
    event_id: int
    type: str
    day: date
    change: str
    window_days: int
    before: dict
    after: dict
    verdict: str


class ReviewOut(BaseModel):
    items: list[ReviewItem]


@capability("ops.review", "复盘", "对比商品改动前后的转化率（程序计算，不调模型）。", ReviewParams)
def review_changes(ctx: CapContext, p: ReviewParams) -> ReviewOut:
    s = ctx.session
    latest = queries.business_day(s)
    n = get_settings().rules["ops"]["review_window_days"]
    since = p.since_day or latest - timedelta(days=30)
    items = []
    for ev in queries.change_events(s, p.goods_id, since):
        window = max(1, min(n, (latest - ev.occurred_on).days))  # 观察期未满时用已有天数
        before, after, verdict = review.compare_event(s, ev, window)
        items.append(ReviewItem(event_id=ev.id, type=ev.type, day=ev.occurred_on, change=f"{ev.before} → {ev.after}",
                                window_days=window, before=before, after=after,
                                verdict=verdict if window == n else f"{verdict}（观察期 {window}/{n} 天）"))
    return ReviewOut(items=items)


# ---------- CEO ----------

class AskParams(BaseModel):
    question: str = Field(description="自然语言问题")


@capability("ceo.ask", "问答", "基于最新数据回答问题（只读，不派活）。", AskParams, needs=(_LLM,))
def ask(ctx: CapContext, p: AskParams) -> ceo.ChatOut:
    out = ceo.chat(ctx.runner, ctx.session, p.question)
    return ceo.ChatOut(reply=out.reply, instruction="")  # 单独任务里不接受指令


# ---------- 执行 ----------

class CopyParams(BaseModel):
    goods_id: str = Field("", description="商品 ID（与 name 二选一）")
    name: str = Field("", description="商品名称")
    instruction: str = Field(description="要求，例如 突出无烟、送礼")


@capability("exec.copy", "写文案", "按要求生成标题与卖点草稿。", CopyParams, needs=(_LLM,), promote_to="suggestion")
def write_copy(ctx: CapContext, p: CopyParams) -> executors.CopyOut:
    prod = ctx.session.scalar(select(Product).where(Product.goods_id == p.goods_id)) if p.goods_id else None
    name = prod.name if prod else p.name
    if not name:
        raise ValueError("请给出商品 ID 或商品名称")
    return ctx.runner.run(executors.COPY, f"为商品 {name} 改写标题：{p.instruction}",
                          context={"goods_id": p.goods_id, "name": name})


class ImageParams(BaseModel):
    goods_id: str
    instruction: str = Field(description="要求，例如 换成卧室场景背景")


class ImageOut(BaseModel):
    goods_id: str
    path: str
    instruction: str


@capability("exec.image", "修图", "生成主图新版本草稿（不换店铺主图）。", ImageParams, needs=(("image_gen",),),
            promote_to="suggestion")
def edit_image(ctx: CapContext, p: ImageParams) -> ImageOut:
    prod = ctx.session.scalar(select(Product).where(Product.goods_id == p.goods_id))
    if prod is None:
        raise ValueError(f"商品 {p.goods_id} 不存在")
    if ctx.services.image_gen is None:
        raise RuntimeError("生图服务未接入（能力 image_gen）")
    new = ctx.services.image_gen.replace_background(Path(f"{prod.main_image_hash}.jpg"), p.instruction)
    return ImageOut(goods_id=p.goods_id, path=str(new), instruction=p.instruction)


class ListingParams(BaseModel):
    offer: str = Field(description="1688 链接或 offer id")
    keyword: str = Field(description="核心需求词")
    sell_price_fen: int = Field(gt=0)


class ListingOut(BaseModel):
    package: dict
    selling_points: list[str]


@capability("exec.listing", "打包上架物料", "生成上架物料包草稿（不推送铺货工具）。", ListingParams,
            needs=(_LLM, _SOURCING), uses_web={"alibaba"})
def listing_package(ctx: CapContext, p: ListingParams) -> ListingOut:
    d: OfferDetail = _source(ctx).offer_detail(_offer_id(p.offer))
    copy = ctx.runner.run(executors.COPY, f"为 1688 货源「{d.title}」写拼多多上新标题与卖点，核心需求词：{p.keyword}",
                          context={"goods_id": d.offer_id, "name": p.keyword})
    pkg = ListingPackage(
        title=copy.title, main_images=d.image_urls[:5],
        skus=[{"name": "默认", "price_fen": p.sell_price_fen, "source_offer_id": d.offer_id}],
        price_fen=p.sell_price_fen,
        source_offer_url=f"https://detail.1688.com/offer/{d.offer_id.removeprefix('1688_')}.html",
    )
    out_dir = get_settings().path("packages_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"adhoc_{d.offer_id}.json"
    path.write_text(pkg.model_dump_json(indent=2), encoding="utf-8")
    pkg.path = str(path)
    return ListingOut(package=pkg.model_dump(), selling_points=copy.selling_points)


def missing_capabilities(s: Session, cap: AgentCapability) -> list[str]:
    """该能力当前缺哪些能力开关（每组都要有一个 healthy）。"""
    from app import capabilities as caps

    return ["/".join(group) for group in cap.needs if not any(caps.is_healthy(s, k) for k in group)]

