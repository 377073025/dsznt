"""运营 Agent：看数诊断 → 给建议（只读）。数字全部由程序算好放进事实表，模型只解读。"""
from __future__ import annotations

from datetime import date, timedelta

from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.agents.facts import FactTable, check_numbers
from app.agents.runtime import AgentRunner, AgentSpec
from app.config import get_settings
from app.data import queries
from app.models import Shop, Suggestion
from app.rules.metrics import compare, cvr_anomaly


class SuggestionOut(BaseModel):
    goods_id: str
    type: str          # main_image / title / price / promotion / other
    action: str        # 「建议……」
    reason: str        # 「因为……」
    evidence: list[str]
    confidence: str    # high / medium / low
    needs_write: bool


class OpsDiagnosis(BaseModel):
    summary: str
    suggestions: list[SuggestionOut]


def _text(o: OpsDiagnosis) -> str:
    return "\n".join([o.summary] + [f"{x.action} {x.reason} {' '.join(x.evidence)}" for x in o.suggestions])


def _drop_invalid(o: OpsDiagnosis, facts: FactTable) -> OpsDiagnosis:
    keep = [x for x in o.suggestions if not check_numbers(f"{x.action} {x.reason} {' '.join(x.evidence)}", facts)]
    summary = o.summary if not check_numbers(o.summary, facts) else "（摘要中的数字未通过校验，已省略）"
    return OpsDiagnosis(summary=summary, suggestions=keep)


SPEC = AgentSpec(
    name="ops.diagnose",
    model_role="main",
    output=OpsDiagnosis,
    tool_names=["get_metrics", "get_change_events"],
    drop_invalid=_drop_invalid,
    numeric_text=_text,
)


def build_input(s: Session, day: date, days: int = 7) -> tuple[str, FactTable, dict]:
    cfg = get_settings().rules["ops"]
    rows = queries.day_rows(s, day, days * 2)
    facts = FactTable()
    facts.add("window.days", days)
    goods_ctx, lines = [], []
    for p in queries.products(s):
        facts.ignore_strings.update({p.name, p.title})
        c = compare(rows, p.goods_id, day, days)
        k = p.goods_id
        facts.add(f"{k}.visitors_cur", c.current.visitors)
        facts.add(f"{k}.orders_cur", c.current.orders)
        facts.add(f"{k}.cvr_cur_pct", round(c.current.cvr * 100, 1))
        facts.add(f"{k}.visitors_prev", c.previous.visitors)
        facts.add(f"{k}.orders_prev", c.previous.orders)
        facts.add(f"{k}.cvr_prev_pct", round(c.previous.cvr * 100, 1))
        anomaly = cvr_anomaly(c, cfg["min_weekly_visitors_for_cvr"], cfg["cvr_drop_alert_ratio"])
        events = queries.change_events(s, k, day - timedelta(days=days * 2))
        ev_ctx = [{"type": e.type, "day": e.occurred_on.isoformat(), "before": e.before, "after": e.after} for e in events]
        facts.ignore_strings.update(e.before for e in events)
        facts.ignore_strings.update(e.after for e in events)
        goods_ctx.append({"goods_id": k, "name": p.name, "anomaly": anomaly, "change_events": ev_ctx})
        lines.append(
            f"- {p.name}（{k}）：近{days}天 访客 {c.current.visitors}、下单 {c.current.orders}、转化率 {facts.values[f'{k}.cvr_cur_pct']}%；"
            f"前{days}天 访客 {c.previous.visitors}、下单 {c.previous.orders}、转化率 {facts.values[f'{k}.cvr_prev_pct']}%"
            f"{'【转化率异常】' if anomaly else ''}"
            + ("；改动事件：" + "；".join(f"{e['day']} {e['type']} {e['before']} → {e['after']}" for e in ev_ctx) if ev_ctx else "")
        )
    content = (
        f"数据日期：{day.isoformat()}。以下数字均由程序计算，只能引用这些数字，不要自己估算。\n"
        f"单款周访客低于 {cfg['min_weekly_visitors_for_cvr']} 时不判断转化率异常；"
        "没有匹配的改动事件时只写现象，置信度标 low，不要编造原因。\n\n" + "\n".join(lines)
    )
    facts.add("rule.min_visitors", cfg["min_weekly_visitors_for_cvr"])
    return content, facts, {"goods": goods_ctx, "day": day.isoformat()}


def run(runner: AgentRunner, s: Session, shop: Shop, day: date) -> list[Suggestion]:
    content, facts, ctx = build_input(s, day)
    out = runner.run(SPEC, content, facts=facts, context=ctx)
    limit = get_settings().rules["ops"]["max_suggestions_per_report"]
    created = []
    for x in out.suggestions[:limit]:
        sug = Suggestion(
            shop_id=shop.id, goods_id=x.goods_id, day=day, type=x.type, action=x.action, reason=x.reason,
            evidence=x.evidence, confidence=x.confidence, needs_write=x.needs_write,
        )
        s.add(sug)
        created.append(sug)
    s.flush()
    return created
