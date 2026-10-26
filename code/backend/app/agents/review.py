"""结果回流：到期复盘（改动前后对比）与 14 天测款判定。数字和结论都由程序给出。"""
from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.data import queries
from app.models import ChangeEvent, Review, Suggestion, Task, TestRun
from app.rules.metrics import window
from app.rules.selection import trial_verdict
from app.workflow.state_machine import SYSTEM, transition

KIND_LABEL = {"main_image": "主图", "title": "标题", "price": "价格", "sku": "SKU"}


def _verdict(before: float, after: float) -> str:
    if before == 0:
        return "无法判断"
    if after >= before * 1.1:
        return "有效"
    if after <= before * 0.9:
        return "变差"
    return "无明显变化"


def run_due_reviews(s: Session, day: date) -> list[Review]:
    """改动满观察期（默认 7 天）后，对比改动前后各 N 天的转化率。"""
    n = get_settings().rules["ops"]["review_window_days"]
    reviewed = set(s.scalars(select(Review.change_event_id)))
    events = s.scalars(select(ChangeEvent).where(ChangeEvent.occurred_on <= day - timedelta(days=n))).all()
    out = []
    for ev in events:
        if ev.id in reviewed:
            continue
        rows = [r for r in queries.day_rows(s, ev.occurred_on + timedelta(days=n), n * 2 + 1) if r.goods_id == ev.goods_id]
        before = window(rows, ev.occurred_on - timedelta(days=1), n)
        after = window(rows, ev.occurred_on + timedelta(days=n), n)
        rv = Review(
            change_event_id=ev.id, window_days=n,
            before={"visitors": before.visitors, "orders": before.orders, "cvr_pct": round(before.cvr * 100, 1)},
            after={"visitors": after.visitors, "orders": after.orders, "cvr_pct": round(after.cvr * 100, 1)},
            verdict=_verdict(before.cvr, after.cvr),
        )
        s.add(rv)
        out.append(rv)
        if ev.task_id:
            task = s.get(Task, ev.task_id)
            if task and task.ref_type == "suggestion":
                sug = s.get(Suggestion, task.ref_id)
                if sug and sug.status == "done":
                    transition(s, "suggestion", sug.id, "review", SYSTEM, {"verdict": rv.verdict})
    s.flush()
    return out


def describe(s: Session, rv: Review) -> str:
    ev = s.get(ChangeEvent, rv.change_event_id)
    names = {p.goods_id: p.name for p in queries.products(s)}
    return (f"{names.get(ev.goods_id, ev.goods_id)} {ev.occurred_on.isoformat()} 改{KIND_LABEL.get(ev.type, ev.type)}："
            f"前 {rv.window_days} 天转化率 {rv.before['cvr_pct']}% → 后 {rv.window_days} 天 {rv.after['cvr_pct']}%，{rv.verdict}")


def run_due_trials(s: Session, day: date) -> list[tuple[TestRun, float]]:
    """测款满 14 天：按访客与相对店铺平均转化率给出 keep / optimize / drop，等店主确认。

    返回 (测款记录, 同期店铺其他商品的平均转化率)。
    """
    cfg = get_settings().rules["selection"]["test_run"]
    days = cfg["days"]
    due = s.scalars(select(TestRun).where(TestRun.verdict == "", TestRun.start_date <= day - timedelta(days=days - 1))).all()
    out = []
    for tr in due:
        end = tr.start_date + timedelta(days=days - 1)
        rows = queries.day_rows(s, end, days)
        mine = window([r for r in rows if r.goods_id == tr.goods_id], end, days)
        shop = window([r for r in rows if r.goods_id != tr.goods_id], end, days)
        tr.visitors, tr.cvr = mine.visitors, round(mine.cvr, 4)
        tr.verdict = trial_verdict(mine.visitors, mine.cvr, shop.cvr, cfg)
        out.append((tr, shop.cvr))
    s.flush()
    return out
