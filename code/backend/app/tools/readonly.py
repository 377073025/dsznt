"""只读工具：分析、选品类智能体可用。"""
from __future__ import annotations

from datetime import date

from pydantic import BaseModel

from app.data import queries
from app.models import Task
from app.rules.metrics import compare
from app.tools.registry import ToolContext, tool


class GoodsWindowArgs(BaseModel):
    goods_id: str
    end_day: str  # YYYY-MM-DD
    days: int


@tool("get_metrics", "读取某商品截至 end_day 的近 days 天与前 days 天指标（访客、加购、下单、转化率）。", GoodsWindowArgs, "readonly")
def get_metrics(ctx: ToolContext, a: GoodsWindowArgs) -> dict:
    end = date.fromisoformat(a.end_day)
    c = compare(queries.day_rows(ctx.session, end, a.days * 2), a.goods_id, end, a.days)
    def w(x):
        return {"visitors": x.visitors, "add_to_cart": x.add_to_cart, "orders": x.orders, "cvr_pct": round(x.cvr * 100, 1)}
    return {"goods_id": a.goods_id, "current": w(c.current), "previous": w(c.previous)}


class ChangeEventsArgs(BaseModel):
    goods_id: str
    since_day: str


@tool("get_change_events", "读取某商品自 since_day 起的改动事件（标题、主图、价格）。", ChangeEventsArgs, "readonly")
def get_change_events(ctx: ToolContext, a: ChangeEventsArgs) -> list[dict]:
    evs = queries.change_events(ctx.session, a.goods_id, date.fromisoformat(a.since_day))
    return [{"type": e.type, "before": e.before, "after": e.after, "day": e.occurred_on.isoformat(), "source": e.source} for e in evs]


class TaskArgs(BaseModel):
    task_id: int


@tool("get_task_output", "读取任务的执行产物与验收清单。", TaskArgs, "readonly")
def get_task_output(ctx: ToolContext, a: TaskArgs) -> dict:
    t = ctx.session.get(Task, a.task_id)
    if t is None:
        return {"error": "任务不存在"}
    return {"executor": t.executor, "spec": t.spec, "output": t.output, "checklist": t.checklist}


class TopGoodsArgs(BaseModel):
    end_day: str
    days: int


@tool("get_top_goods", "按近 days 天订单数列出本店商品。", TopGoodsArgs, "readonly")
def get_top_goods(ctx: ToolContext, a: TopGoodsArgs) -> list[dict]:
    end = date.fromisoformat(a.end_day)
    rows = queries.day_rows(ctx.session, end, a.days)
    totals: dict[str, int] = {}
    for r in rows:
        totals[r.goods_id] = totals.get(r.goods_id, 0) + r.orders
    names = {p.goods_id: p.name for p in queries.products(ctx.session)}
    return [{"goods_id": g, "name": names.get(g, g), "orders": n} for g, n in sorted(totals.items(), key=lambda x: -x[1])]
