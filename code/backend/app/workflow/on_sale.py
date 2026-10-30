"""在售款：上新商品 ID 回填、货源每日复查 offer_watch（07 号文档第 10、13 节）。"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import selection
from app.config import get_settings
from app.db import utcnow
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import send_card
from app.models import Asset, Candidate, Offer, Product, ProductSnapshot, ProcessedAction, Task, TestRun
from app.rules import selection as R
from app.services import Services
from app.workflow.events import audit
from app.workflow.jobs import JobContext, job_handler

log = logging.getLogger(__name__)


# ---------- 上新商品 ID 回填 ----------

def _norm(title: str) -> str:
    return "".join(title.split())


def backfill_listed_ids(s: Session, day: date) -> list[tuple[int, str]]:
    """测款记录里的商品 ID 先记的是铺货工具回执；当天快照里出现标题与物料包一致的新商品时，
    换成拼多多商品 ID，并把货源记到商品上（offer_watch 据此复查）。返回 [(候选 ID, 商品 ID)]。"""
    known = set(s.scalars(select(Product.goods_id)))
    pending = [tr for tr in s.scalars(select(TestRun).order_by(TestRun.id)) if tr.goods_id not in known]
    if not pending:
        return []
    taken = set(s.scalars(select(TestRun.goods_id))) & known  # 已经对应某次测款的商品
    snaps = s.scalars(select(ProductSnapshot).where(ProductSnapshot.day == day)).all()
    out: list[tuple[int, str]] = []
    for cid in dict.fromkeys(tr.candidate_id for tr in pending):
        task = s.scalar(select(Task).where(Task.ref_type == "candidate", Task.ref_id == cid, Task.executor == "listing")
                        .order_by(Task.id.desc()))
        title = _norm((task.output.get("package") or {}).get("title", "")) if task else ""
        if not title:
            continue
        # 铺货工具或平台可能在标题前后加字：完全一致优先，其次互相包含（至少 8 个字）
        hits = [x.goods_id for x in snaps if x.goods_id not in taken and _norm(x.title) == title] or [
            x.goods_id for x in snaps if x.goods_id not in taken and len(title) >= 8
            and (title in _norm(x.title) or _norm(x.title) in title)]
        if len(set(hits)) != 1:
            continue
        gid = hits[0]
        cand = s.get(Candidate, cid)
        for tr in s.scalars(select(TestRun).where(TestRun.candidate_id == cid)):
            if tr.goods_id not in known:
                tr.goods_id = gid
        prod = s.scalar(select(Product).where(Product.goods_id == gid))
        prod.source_offer_id = cand.offer_id
        for a in s.scalars(select(Asset).where(Asset.goods_id == cand.offer_id)):
            a.goods_id = gid
        audit(s, "system:system", "candidate.goods_id_backfill", f"candidate:{cid}", {"goods_id": gid})
        taken.add(gid)
        out.append((cid, gid))
    return out


# ---------- 在售款货源复查 ----------

def _alert_once(s: Session, key: str) -> bool:
    if s.get(ProcessedAction, key) is not None:
        return False
    s.add(ProcessedAction(key=key))
    return True


def find_backups(s: Session, source, prod: Product, exclude: str, limit: int) -> list[dict[str, Any]]:
    """备选货源：先用选品时记下的备选（已做过同款与合规审查），不够再以图搜同款、按硬性过滤筛一遍。"""
    rules = get_settings().rules["selection"]
    out: list[dict[str, Any]] = []
    cand = s.scalar(select(Candidate).join(TestRun, TestRun.candidate_id == Candidate.id)
                    .where(TestRun.goods_id == prod.goods_id).order_by(Candidate.id.desc()))
    seen = {exclude}
    for oid in cand.backup_offer_ids if cand else []:
        o = s.get(Offer, oid)
        if o is not None and oid not in seen:
            seen.add(oid)
            out.append({"offer_id": oid, "title": o.title, "price_fen": o.price_fen, "checked": True})
    found = []
    for brief in source.search_by_image(Path(f"{prod.main_image_hash}.jpg"), limit=10):
        if len(out) + len(found) >= limit:
            break
        if brief.offer_id in seen:
            continue
        seen.add(brief.offer_id)
        try:
            d = source.offer_detail(brief.offer_id)
        except LookupError:
            continue
        if R.hard_filter(d, prod.price_fen, rules["hard_filters"], rules["profit"]):
            continue
        found.append({"offer_id": d.offer_id, "title": d.title, "price_fen": d.price_fen, "checked": False})
    return (out + sorted(found, key=lambda x: x["price_fen"]))[:limit]


def watch_offers(s: Session, services: Services, day: date) -> list[dict[str, Any]]:
    """逐个复查在售商品的货源：下架、不再代发、涨价、库存不足、按现售价已不赚钱 → 当天飞书告警一次并列出备选货源。

    价格对比的基准是上次告警（或第一次复查）时的拿货价，所以缓慢上涨累计超过阈值也会告警，告警后基准更新。"""
    source = selection.sourcing(s, services)
    if source is None:
        log.warning("offer_watch：1688 读取能力不可用，跳过")
        return []
    rules = get_settings().rules["selection"]
    hf, cfg = rules["hard_filters"], rules["offer_watch"]
    alerts = []
    for prod in s.scalars(select(Product).where(Product.source_offer_id != "").order_by(Product.id)):
        oid = prod.source_offer_id
        offer = s.get(Offer, oid)
        try:
            d = source.offer_detail(oid)
        except LookupError:
            d = None
        issues = []
        if d is None:
            issues.append("货源已下架或无法访问")
        else:
            base = (offer.raw or {}).get("watch_base_fen") if offer else None
            base = base or (offer.price_fen if offer else d.price_fen)
            if not d.dropship:
                issues.append("不再支持一件代发")
            if d.price_fen > base * (1 + cfg["price_rise_ratio"]):
                issues.append(f"拿货价 {base / 100:.2f} → {d.price_fen / 100:.2f} 元（涨 {d.price_fen / base - 1:.0%}）")
            if d.stock < hf["min_stock"]:
                issues.append(f"库存 {d.stock}，低于 {hf['min_stock']}")
            p = R.profit(prod.price_fen, d, rules["profit"])
            if p.margin < hf["min_margin"] or p.profit_fen < hf["min_profit_per_unit"] * 100:
                issues.append(f"按现售价 {prod.price_fen / 100:.2f} 元，单件利润 {p.profit_fen / 100:.2f} 元、毛利率 {p.margin:.0%}，低于规则")
            offer = selection._upsert_offer(s, d)
            raw = dict(offer.raw)
            raw["watch_base_fen"] = d.price_fen if issues else base
            offer.raw = raw
        if not issues or not _alert_once(s, f"offer_watch:{oid}:{day.isoformat()}"):
            continue
        backups = find_backups(s, source, prod, oid, cfg["max_backups"])
        alerts.append({"goods_id": prod.goods_id, "name": prod.name, "offer_id": oid, "issues": issues, "backups": backups})
        send_card(s, services, cards.offer_alert(prod.name, oid, issues, backups), "product", prod.id)
    return alerts


EXPIRE_WARN_DAYS = 14


def check_dropship(s: Session, services: Services, day: date) -> list[str]:
    """代发助手王每日检查：店铺授权、软件到期、待采购订单、货源下架、在售商品没关联货源 → 当天飞书提醒一次。
    已关联货源而系统里还没记货源的商品，顺手记上（offer_watch 据此复查价格和库存）。"""
    from app import capabilities as caps

    if not hasattr(services.dropship, "status") or not caps.is_healthy(s, "dropship_tool"):
        return []
    st = services.dropship.status()
    shop, issues = st["shop"], []

    def soon(d: str) -> int | None:
        return (date.fromisoformat(d) - day).days if d else None

    if not shop["authorized"]:
        issues.append("拼多多店铺在代发助手王里的授权已失效，铺货和抓单都会停：请到「我的 → 我的店铺」重新授权")
    elif (left := soon(shop["auth_expire"])) is not None and left <= EXPIRE_WARN_DAYS:
        issues.append(f"拼多多店铺授权 {shop['auth_expire']} 到期（还剩 {left} 天）")
    if (left := soon(st["service_end"])) is not None and left <= EXPIRE_WARN_DAYS:
        issues.append(f"代发助手王 {st['service_end']} 到期（还剩 {left} 天），到期后不能铺货和采购")
    if st["orders"] and st["orders"]["wait_purchase"]:
        issues.append(f"{st['orders']['wait_purchase']} 笔订单待采购：开了自动采购的会自动下单，没关联货源的要手动采购")
    for g in st["linked"]:
        if g["source_status"] != "published":
            issues.append(f"货源已不在售（{g['source_status']}）：{g['title'][:20]}（商品 {g['goods_id']}）")
    if st["unlinked"]:
        names = "、".join(x["name"][:16] for x in st["unlinked"][:5])
        issues.append(f"{len(st['unlinked'])} 个在售商品没关联货源，来了订单不会自动采购：{names}")
    products = {p.goods_id: p for p in s.scalars(select(Product))}
    for g in st["linked"]:
        prod = products.get(g["goods_id"])
        if prod is not None and not prod.source_offer_id:
            prod.source_offer_id = g["offer_id"]
            audit(s, "system", "product.source_from_dropship", f"goods:{prod.goods_id}", {"offer_id": g["offer_id"]})
    if issues and _alert_once(s, f"dropship_check:{day.isoformat()}"):
        send_card(s, services, cards.notice("代发助手王 · 每日检查", "\n".join(f"- {x}" for x in issues)), "system", 0)
    return issues


@job_handler("fetch.offer_watch")
def _offer_watch(ctx: JobContext, p: dict[str, Any]) -> None:
    day = date.fromisoformat(p["day"]) if p.get("day") else (utcnow() + timedelta(hours=8)).date()
    try:
        check_dropship(ctx.session, ctx.services, day)
    except Exception as e:  # noqa: BLE001  代发检查失败不影响货源复查
        log.warning("代发助手王每日检查失败：%s", e)
    watch_offers(ctx.session, ctx.services, day)
