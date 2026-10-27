"""Web 控制台其余页面的接口（02 号文档 7.2 节）：闭环总览、选品中心、任务看板、数据中心、规则配置、成本、
用户与权限、商品与素材。

只读查询为主；会改状态的操作（拍板、改一改、恢复采集、改规则）都按角色校验，且和飞书按钮走同一条路径。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import capabilities as caps
from app.config import get_settings
from app.data import queries, router as data_router
from app.data.items import load_items
from app.db import session_scope, utcnow
from app.interfaces.feishu.handler import ROLES, get_or_register, set_role
from app.llm.gateway import Gateway
from app.models import (Asset, Capability, Candidate, ChangeEvent, Demand, FetchJob, LlmBatch, LlmUsage, ManualImport, Offer, Product,
                        Report, Review, RiskEvent, RuleProposal, Suggestion, Task, TestRun, User)
from app.workflow import rule_config
from app.workflow.jobs import enqueue
from app.workflow.state_machine import MODELS, Actor, can

api = APIRouter(prefix="/api")


def db() -> Iterator[Session]:
    with session_scope() as s:
        yield s


def current_user(x_open_id: str = Header(default=""), s: Session = Depends(db)) -> User:
    if not x_open_id:
        raise HTTPException(401, "缺少 X-Open-Id")
    return get_or_register(s, x_open_id)


def _cn_today() -> date:
    return (utcnow() + timedelta(hours=8)).date()


def _cn_date(dt: datetime) -> str:
    return (dt + timedelta(hours=8)).date().isoformat()


def _count(items: Any, key: Callable[[Any], str]) -> dict[str, int]:
    return dict(Counter(key(x) for x in items))


# ---------- 闭环总览 ----------

CAND_PASSED = {"recommended", "revising", "approved", "preparing", "listed", "testing", "kept", "optimizing", "delisted"}


def _funnel(cands: list[Candidate]) -> dict[str, int]:
    """选品漏斗：按淘汰原因倒推每一关通过了多少（候选只记最终状态）。"""
    rejected = [c for c in cands if c.status == "rejected"]
    by_reason = Counter(
        "same" if c.reject_reason.startswith(("同款", "合规")) else "score" if c.reject_reason.startswith("得分")
        else "owner" if not c.reject_reason else "filter" for c in rejected)
    total = len(cands)
    filtered = total - by_reason["filter"]
    verified = filtered - by_reason["same"]
    return {"sourced": total, "filtered": filtered, "verified": verified,
            "recommended": sum(c.status in CAND_PASSED for c in cands) + by_reason["owner"],
            "testing": sum(c.status in ("testing", "optimizing") for c in cands),
            "kept": sum(c.status == "kept" for c in cands)}


def latest_week(s: Session) -> str:
    return s.scalar(select(Demand.week).order_by(Demand.week.desc()).limit(1)) or ""


@api.get("/overview")
def overview(s: Session = Depends(db)) -> dict[str, Any]:
    day = queries.business_day(s)
    jobs = s.scalars(select(FetchJob).where(FetchJob.day == day)).all()
    sugs_today = s.scalars(select(Suggestion).where(Suggestion.day == day)).all()
    report = s.scalar(select(Report).where(Report.kind == "daily", Report.period == day.isoformat()))
    tasks = s.scalars(select(Task).where(Task.updated_at >= utcnow() - timedelta(days=7))).all()
    reviews = s.scalars(select(Review).where(Review.at >= utcnow() - timedelta(days=7))).all()
    pending = s.scalars(select(Suggestion).where(Suggestion.status.in_(("reported", "revising")))
                        .order_by(Suggestion.id.desc())).all()
    week = latest_week(s)
    cands = s.scalars(select(Candidate).join(Demand, Demand.id == Candidate.demand_id).where(Demand.week == week)).all()
    loop = [
        {"step": "采集", "detail": _count(jobs, lambda j: j.status), "ok": bool(jobs) and all(j.status == data_router.SUCCESS for j in jobs)},
        {"step": "指标", "detail": {"商品": len(queries.day_rows(s, day, 1))}, "ok": bool(queries.day_rows(s, day, 1))},
        {"step": "诊断", "detail": {"建议": len(sugs_today)}, "ok": report is not None},
        {"step": "早报", "detail": {"已生成": 1 if report else 0}, "ok": report is not None},
        {"step": "拍板", "detail": _count(sugs_today, lambda x: x.status), "ok": not any(x.status == "reported" for x in sugs_today)},
        {"step": "执行", "detail": _count(tasks, lambda t: t.status), "ok": not any(t.status in ("failed", "escalated") for t in tasks)},
        {"step": "复盘", "detail": _count(reviews, lambda r: r.verdict), "ok": True},
    ]
    return {
        "day": day.isoformat(), "loop": loop, "week": week, "funnel": _funnel(list(cands)),
        "demands": s.scalar(select(Demand.id).where(Demand.week == week).limit(1)) is not None,
        "capabilities": [{"key": c.key, "title": caps.CAPABILITIES[c.key].title, "status": c.status}
                         for c in s.scalars(select(Capability))],
        "pending_suggestions": [_suggestion(x) for x in pending],
        "report": report.content if report else None,
    }


def _suggestion(x: Suggestion) -> dict[str, Any]:
    return {"id": x.id, "day": x.day.isoformat(), "goods_id": x.goods_id, "type": x.type, "action": x.action,
            "reason": x.reason, "evidence": x.evidence, "confidence": x.confidence, "needs_write": x.needs_write,
            "status": x.status, "version": x.version, "revise_rounds": x.revise_rounds}


# ---------- 拍板与改一改（Web 与飞书同一条路径） ----------

class ReviseIn(BaseModel):
    feedback: str


@api.post("/{entity}/{obj_id}/revise")
def revise(entity: str, obj_id: int, body: ReviseIn, s: Session = Depends(db), user: User = Depends(current_user)) -> dict:
    """「改一改」：先把状态改成 revising（card.apply），再按意见改写（revise.run），改完推送新卡片、回到待拍板。"""
    if entity not in ("suggestion", "candidate") or not body.feedback.strip():
        raise HTTPException(400, "请写修改意见")
    obj = s.get(MODELS[entity], obj_id)
    if obj is None:
        raise HTTPException(404, "对象不存在")
    actor = Actor("user", user.feishu_open_id, user.role)
    if obj.status != "revising":
        if not can(entity, obj.status, "revise", actor):
            raise HTTPException(403, f"无权在状态 {obj.status} 修改")
        enqueue(s, "card.apply", {"open_id": user.feishu_open_id, "entity": entity, "id": obj_id, "event": "revise"},
                dedupe_key=f"card:{entity}:{obj_id}:revise:{obj.version}")
    elif user.role != "owner":
        raise HTTPException(403, "只有店主可以修改")
    enqueue(s, "revise.run", {"entity": entity, "id": obj_id, "feedback": body.feedback.strip()})
    return {"queued": True}


# ---------- 选品中心 ----------

@api.get("/candidates")
def candidates(week: str = "", s: Session = Depends(db)) -> dict[str, Any]:
    weeks = list(s.scalars(select(Demand.week).distinct().order_by(Demand.week.desc()).limit(12)))
    week = week or (weeks[0] if weeks else "")
    rows = s.execute(select(Candidate, Demand, Offer).join(Demand, Demand.id == Candidate.demand_id)
                     .join(Offer, Offer.id == Candidate.offer_id).where(Demand.week == week)
                     .order_by(Candidate.score.desc())).all()
    titles = {o.id: o.title for o in s.scalars(select(Offer).where(
        Offer.id.in_({b for c, _, _ in rows for b in c.backup_offer_ids})))}
    return {"week": week, "weeks": weeks, "funnel": _funnel([c for c, _, _ in rows]), "candidates": [{
        "id": c.id, "status": c.status, "version": c.version, "score": c.score, "breakdown": c.breakdown,
        "price_plan": c.price_plan, "reason": c.reason, "risks": c.risks, "reject_reason": c.reject_reason,
        "same_conf": c.same_conf, "compliance": c.compliance, "revise_rounds": c.revise_rounds,
        "backups": [{"offer_id": b, "title": titles.get(b, b)} for b in c.backup_offer_ids],
        "demand": {"keyword": d.keyword, "seed_type": d.seed_type, "seed_ref": d.seed_ref, "heat": d.heat, "trend": d.trend},
        "offer": {"id": o.id, "title": o.title, "price_fen": o.price_fen, "supplier_years": o.supplier_years,
                  "ship_48h_rate": o.ship_48h_rate, "images": o.image_urls[:3]},
    } for c, d, o in rows]}


@api.get("/test-runs")
def test_runs(s: Session = Depends(db)) -> list[dict[str, Any]]:
    rows = s.execute(select(TestRun, Candidate, Offer).join(Candidate, Candidate.id == TestRun.candidate_id)
                     .join(Offer, Offer.id == Candidate.offer_id).order_by(TestRun.id.desc()).limit(50)).all()
    day = queries.business_day(s)
    return [{"id": t.id, "candidate_id": c.id, "candidate_status": c.status, "candidate_version": c.version,
             "title": o.title, "goods_id": t.goods_id, "start_date": t.start_date.isoformat(), "days": t.days,
             "trial_round": t.trial_round, "observed": max(0, min(t.days, (day - t.start_date).days + 1)),
             "visitors": t.visitors, "cvr": t.cvr, "verdict": t.verdict} for t, c, o in rows]


# ---------- 任务看板 ----------

BOARD = [("pending", "待执行"), ("running", "执行中"), ("confirm", "待写入确认"), ("submitted", "待验收"),
         ("accepted", "完成"), ("problem", "失败 / 上报")]
_COLUMN = {"pending": "pending", "running": "running", "produced": "running", "awaiting_write_confirm": "confirm",
           "writing": "confirm", "submitted": "submitted", "accepted": "accepted", "failed": "problem", "escalated": "problem"}


@api.get("/board")
def board(days: int = 14, s: Session = Depends(db)) -> dict[str, Any]:
    tasks = s.scalars(select(Task).where(Task.updated_at >= utcnow() - timedelta(days=days)).order_by(Task.id.desc())).all()
    sug_ids = {t.ref_id for t in tasks if t.ref_type == "suggestion"}
    cand_ids = {t.ref_id for t in tasks if t.ref_type == "candidate"}
    sugs = {x.id: x.action for x in s.scalars(select(Suggestion).where(Suggestion.id.in_(sug_ids)))}
    cands = {c.id: o.title for c, o in s.execute(select(Candidate, Offer).join(Offer, Offer.id == Candidate.offer_id)
                                                 .where(Candidate.id.in_(cand_ids)))}
    cols: dict[str, list[dict[str, Any]]] = {k: [] for k, _ in BOARD}
    for t in tasks:
        ref = sugs.get(t.ref_id, "") if t.ref_type == "suggestion" else cands.get(t.ref_id, "")
        out = t.output or {}
        cols[_COLUMN.get(t.status, "running")].append({
            "id": t.id, "executor": t.executor, "status": t.status, "version": t.version, "needs_write": t.needs_write,
            "ref": f"{'建议' if t.ref_type == 'suggestion' else '候选'} #{t.ref_id} {ref}", "instruction": t.spec.get("instruction", ""),
            "goods_id": t.spec.get("goods_id", ""), "rework_count": t.rework_count, "checklist": t.checklist,
            "output": {k: out[k] for k in ("patch", "preview", "write_ref", "selling_points") if k in out}
            | ({"package": {k: out["package"].get(k) for k in ("title", "price_fen", "path")}} if "package" in out else {}),
            "updated_at": t.updated_at.isoformat()})
    return {"columns": [{"key": k, "title": title, "tasks": cols[k]} for k, title in BOARD]}


# ---------- 数据中心 ----------

@api.get("/data/fetch-jobs")
def fetch_jobs(days: int = 14, s: Session = Depends(db)) -> dict[str, Any]:
    end = queries.business_day(s)
    start = end - timedelta(days=days - 1)
    jobs = s.scalars(select(FetchJob).where(FetchJob.day >= start, FetchJob.day <= end).order_by(FetchJob.day.desc())).all()
    items = load_items()
    return {"days": [(end - timedelta(days=i)).isoformat() for i in range(days)],
            "items": [{"key": k, "deadline_minutes": v.deadline_minutes, "channels": [c.type for c in v.channels]}
                      for k, v in items.items()],
            "jobs": [{"id": j.id, "item": j.item, "day": j.day.isoformat(), "status": j.status, "channel": j.channel,
                      "attempts": j.attempts, "rows": j.rows, "error": j.error[:300], "version": j.version}
                     for j in jobs]}


@api.post("/fetch_job/{job_id}/resume")
def resume_fetch(job_id: int, s: Session = Depends(db), user: User = Depends(current_user)) -> dict:
    job = s.get(FetchJob, job_id)
    reason = data_router.resume_denied_reason(job, user.role) if job else "采集任务不存在"
    if reason:
        raise HTTPException(403 if "权限" in reason or "店主" in reason else 400, reason)
    enqueue(s, "card.apply", {"open_id": user.feishu_open_id, "entity": "fetch_job", "id": job_id, "event": "resume"},
            dedupe_key=f"card:fetch_job:{job_id}:resume:{job.version}")
    return {"queued": True}


@api.get("/data/events")
def data_events(days: int = 30, s: Session = Depends(db)) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    names = {p.goods_id: p.name for p in queries.products(s)}
    return {
        "risks": [{"id": r.id, "platform": r.platform, "page": r.page, "type": r.type, "recent_rate": r.recent_rate,
                   "at": r.at.isoformat()} for r in s.scalars(select(RiskEvent).where(RiskEvent.at >= since)
                                                               .order_by(RiskEvent.id.desc()).limit(100))],
        "changes": [{"id": e.id, "goods_id": e.goods_id, "name": names.get(e.goods_id, e.goods_id), "type": e.type,
                     "before": e.before[:120], "after": e.after[:120], "day": e.occurred_on.isoformat(), "source": e.source}
                    for e in s.scalars(select(ChangeEvent).where(ChangeEvent.occurred_on >= (since + timedelta(hours=8)).date())
                                       .order_by(ChangeEvent.occurred_on.desc()).limit(100))],
        "imports": [{"id": m.id, "file": m.file_path.replace("\\", "/").rsplit("/", 1)[-1], "item": m.item, "rows": m.rows,
                     "status": m.status, "errors": m.error_rows[:5], "by": m.uploaded_by, "at": m.created_at.isoformat()}
                    for m in s.scalars(select(ManualImport).order_by(ManualImport.id.desc()).limit(30))],
    }


# ---------- 规则配置 ----------

@api.get("/rules")
def get_rules(s: Session = Depends(db)) -> dict[str, Any]:
    version = rule_config.apply_latest(s)
    hist = rule_config.history(s)
    changes = []
    for newer, older in zip(hist, hist[1:] + [None]):
        base = older.value if older else rule_config._baseline()
        changes.append({"version": newer.rule_version, "by": newer.approved_by, "at": newer.at.isoformat(),
                        "changes": rule_config.diff(base, newer.value)})
    props = s.scalars(select(RuleProposal).order_by(RuleProposal.id.desc()).limit(5)).all()
    return {"version": version, "rules": get_settings().rules, "history": changes,
            "proposals": [{"id": p.id, "period": p.period, "status": p.status, "version": p.version,
                           "summary": p.summary, "rule_version": p.rule_version} for p in props]}


@api.put("/rules")
def put_rules(patch: dict[str, Any], s: Session = Depends(db), user: User = Depends(current_user)) -> dict[str, Any]:
    if user.role != "owner":
        raise HTTPException(403, "只有店主可以修改规则")
    try:
        row = rule_config.save(s, patch, user.feishu_open_id)
    except rule_config.RuleError as e:
        raise HTTPException(400, str(e)) from e
    return {"version": row.rule_version}


# ---------- 成本 ----------

@api.get("/costs")
def costs(days: int = 30, s: Session = Depends(db)) -> dict[str, Any]:
    since = utcnow() - timedelta(days=days)
    rows = s.scalars(select(LlmUsage).where(LlmUsage.at >= since)).all()
    by_day: dict[str, dict[str, float]] = defaultdict(lambda: {"main": 0.0, "adhoc": 0.0})
    agg: dict[str, dict[str, dict[str, float]]] = {"agent": defaultdict(Counter), "model": defaultdict(Counter)}
    batch_saved = 0.0
    for u in rows:
        by_day[_cn_date(u.at)][u.lane] += u.cost_cny
        for dim, key in (("agent", u.agent), ("model", u.model)):
            c = agg[dim][key]
            c["calls"] += 1
            c["tokens"] += u.input_tokens + u.output_tokens
            c["cost"] += u.cost_cny
        if u.batch:
            batch_saved += u.cost_cny  # Batch 半价，省下的钱等于实付
    gw = Gateway(None)
    b = gw.settings.budget
    today = _cn_today()
    return {
        "days": [{"day": (today - timedelta(days=i)).isoformat(),
                  **{k: round(v, 4) for k, v in by_day.get((today - timedelta(days=i)).isoformat(), {"main": 0.0, "adhoc": 0.0}).items()}}
                 for i in range(days - 1, -1, -1)],
        "by_agent": sorted(({"key": k, **{m: round(v, 4) for m, v in c.items()}} for k, c in agg["agent"].items()),
                           key=lambda x: -x["cost"]),
        "by_model": sorted(({"key": k, **{m: round(v, 4) for m, v in c.items()}} for k, c in agg["model"].items()),
                           key=lambda x: -x["cost"]),
        "batch_saved": round(batch_saved, 2),
        "batches": [{"id": x.id, "agent": x.agent, "status": x.status, "at": x.created_at.isoformat()}
                    for x in s.scalars(select(LlmBatch).order_by(LlmBatch.id.desc()).limit(10))],
        "budget": {"daily": b["daily_cny"], "monthly": b["monthly_cny"], "warn_ratio": b["warn_ratio"],
                   "today": round(gw.spent_today(s), 2), "month": round(gw.spent_month(s), 2),
                   "adhoc_daily": gw.settings.adhoc["daily_cny"], "adhoc_today": round(gw.spent_today(s, "adhoc"), 2),
                   "over": gw.over_budget(s), "critical": b["critical_agents"]},
    }


# ---------- 用户与权限 ----------

@api.get("/users")
def list_users(s: Session = Depends(db)) -> dict[str, Any]:
    return {"roles": ROLES, "users": [
        {"id": u.id, "open_id": u.feishu_open_id, "name": u.name, "role": u.role, "created_at": u.created_at.isoformat()}
        for u in s.scalars(select(User).order_by(User.id))]}


class UserPatch(BaseModel):
    role: str | None = None
    name: str | None = None


@api.patch("/users/{user_id}")
def patch_user(user_id: int, body: UserPatch, s: Session = Depends(db), user: User = Depends(current_user)) -> dict[str, Any]:
    target = s.get(User, user_id)
    if target is None:
        raise HTTPException(404, "用户不存在")
    try:
        set_role(s, user, target, body.role or target.role, name=(body.name or "").strip())
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(400, str(e)) from e
    return {"id": target.id, "name": target.name, "role": target.role}


# ---------- 商品与素材 ----------

@api.get("/catalog")
def product_catalog(s: Session = Depends(db)) -> list[dict[str, Any]]:
    """在售商品、各自的货源（offer_watch 复查对象）与素材库版本。"""
    assets = defaultdict(list)
    for a in s.scalars(select(Asset).order_by(Asset.goods_id, Asset.kind, Asset.asset_version.desc())):
        assets[a.goods_id].append({"id": a.id, "kind": a.kind, "version": a.asset_version, "origin": a.origin,
                                   "path": a.path, "note": a.note, "at": a.created_at.isoformat(), "url": _asset_url(a)})
    offers = {o.id: o for o in s.scalars(select(Offer))}
    trials = {t.goods_id: t for t in s.scalars(select(TestRun).order_by(TestRun.id))}
    out = []
    for p in queries.products(s):
        o = offers.get(p.source_offer_id)
        tr = trials.get(p.goods_id)
        out.append({
            "goods_id": p.goods_id, "name": p.name, "title": p.title, "price_fen": p.price_fen,
            "main_image": p.main_image_hash,
            "source": {"offer_id": p.source_offer_id, "title": o.title if o else "", "price_fen": o.price_fen if o else None,
                       "stock": o.stock if o else None, "checked_at": o.updated_at.isoformat() if o else None}
            if p.source_offer_id else None,
            "trial": {"candidate_id": tr.candidate_id, "round": tr.trial_round, "verdict": tr.verdict} if tr else None,
            "assets": assets.get(p.goods_id, []),
        })
    return out


def _asset_file(a: Asset) -> Path | None:
    """素材的本地文件：只认素材库目录里的文件（相对路径按素材库目录算），防止借路径读别的文件。"""
    if a.path.startswith(("http://", "https://")):
        return None
    root = get_settings().path("assets_dir").resolve()
    f = (root / a.path).resolve()
    return f if f.is_file() and f.is_relative_to(root) and f.suffix.lower() in _IMAGE_TYPES else None


_IMAGE_TYPES = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp", ".gif": "image/gif"}


def _asset_url(a: Asset) -> str | None:
    """缩略图地址：网络图片直接用；本地文件走 /api/assets/{id}/file；文件不在了返回 None。"""
    if a.path.startswith(("http://", "https://")):
        return a.path
    return f"/api/assets/{a.id}/file" if _asset_file(a) else None


@api.get("/assets/{asset_id}/file")
def asset_file(asset_id: int, s: Session = Depends(db)) -> FileResponse:
    a = s.get(Asset, asset_id)
    f = _asset_file(a) if a else None
    if f is None:
        raise HTTPException(404, "素材文件不存在")
    return FileResponse(f, media_type=_IMAGE_TYPES[f.suffix.lower()])


class SourceIn(BaseModel):
    offer: str  # 1688 链接或 offer id；空字符串表示不再复查


@api.put("/products/{goods_id}/source")
def put_source(goods_id: str, body: SourceIn, s: Session = Depends(db), user: User = Depends(current_user)) -> dict[str, Any]:
    """登记或更换在售商品的货源（offer_watch 第二天起按新货源复查）。只改本系统记录，不改铺货工具里的对应关系。"""
    from app.agents.catalog import _offer_id
    from app.workflow.events import audit

    if user.role not in ("owner", "operator"):
        raise HTTPException(403, "只有店主和运营可以改货源")
    prod = s.scalar(select(Product).where(Product.goods_id == goods_id))
    if prod is None:
        raise HTTPException(404, "商品不存在")
    raw = body.offer.strip()
    new = _offer_id(raw) if raw else ""
    if new and not new.removeprefix("1688_").isdigit():
        raise HTTPException(400, "请填 1688 商品链接或 offer id")
    audit(s, f"user:{user.feishu_open_id}", "product.set_source", f"product:{goods_id}",
          {"from": prod.source_offer_id, "to": new})
    prod.source_offer_id = new
    return {"goods_id": goods_id, "source_offer_id": new}


# ---------- 手工货源（1688 读取没接通时，由人照着商品页录入） ----------

class ManualOfferIn(BaseModel):
    offer: str                      # 1688 链接或 offer id
    title: str
    category: str = ""
    price_fen: int                  # 一件代发拿货价
    shipping_fen: int = 0
    dropship: bool
    stock: int
    ship_48h_rate: float            # 0–1
    repurchase_rate: float = 0.0
    return_rate: float = 0.0
    category_return_rate: float = 0.0
    supplier_years: int
    weight_kg: float = 0.0
    longest_side_cm: float = 0.0
    image_urls: list[str] = []


def _manual_offer_dict(o: Offer) -> dict[str, Any]:
    return {**{k: v for k, v in o.raw.items() if k not in ("manual",)}, "updated_at": o.updated_at.isoformat()}


@api.get("/offers/manual")
def list_manual_offers(s: Session = Depends(db)) -> list[dict[str, Any]]:
    from app.adapters.alibaba1688.manual import manual_offers

    return [_manual_offer_dict(o) for o in manual_offers(s)]


@api.put("/offers/manual")
def put_manual_offer(body: ManualOfferIn, s: Session = Depends(db), user: User = Depends(current_user)) -> dict[str, Any]:
    """录入或更新一条手工货源。live 通道录过的同一货源也可以覆盖成手工数据（以人看到的为准）。"""
    from app.adapters.types import OfferDetail
    from app.agents import selection
    from app.agents.catalog import _offer_id
    from app.workflow.events import audit

    if user.role not in ("owner", "operator"):
        raise HTTPException(403, "只有店主和运营可以录入货源")
    oid = _offer_id(body.offer)
    if not oid.removeprefix("1688_").isdigit():
        raise HTTPException(400, "请填 1688 商品链接或 offer id")
    for f in ("ship_48h_rate", "repurchase_rate", "return_rate", "category_return_rate"):
        if not 0 <= getattr(body, f) <= 1:
            raise HTTPException(400, f"{f} 要在 0 到 1 之间（百分比请除以 100）")
    if body.price_fen <= 0 or body.stock < 0 or body.supplier_years < 0:
        raise HTTPException(400, "拿货价要大于 0，库存和经营年限不能为负")
    d = OfferDetail.model_validate({**body.model_dump(exclude={"offer"}), "offer_id": oid,
                                    "image_urls": [u.strip() for u in body.image_urls if u.strip()]})
    o = selection._upsert_offer(s, d)
    o.raw = {**o.raw, "manual": True, "entered_by": user.feishu_open_id}
    audit(s, f"user:{user.feishu_open_id}", "offer.manual_put", f"offer:{oid}", {"price_fen": d.price_fen, "stock": d.stock})
    s.flush()
    return _manual_offer_dict(o)
