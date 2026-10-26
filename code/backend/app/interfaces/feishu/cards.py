"""飞书卡片（02 号文档 7.1 节）。按钮的 value 统一为 {entity, id, event, version}，回调时据此走状态机。"""
from __future__ import annotations

from typing import Any

from app.models import Candidate, FetchJob, Report, Suggestion, Task


def _button(text: str, entity: str, obj_id: int, event: str, version: int, kind: str = "default") -> dict[str, Any]:
    return {"tag": "button", "text": {"tag": "plain_text", "content": text}, "type": kind,
            "value": {"entity": entity, "id": obj_id, "event": event, "version": version}}


def _md(text: str) -> dict[str, Any]:
    return {"tag": "markdown", "content": text}


def _card(title: str, elements: list[dict[str, Any]], template: str = "blue") -> dict[str, Any]:
    # update_multi：处理完回调后要更新这张卡片（所有人看到同一份结果）
    return {"config": {"wide_screen_mode": True, "update_multi": True},
            "header": {"title": {"tag": "plain_text", "content": title}, "template": template}, "elements": elements}


def daily_report(report: Report, suggestions: list[Suggestion]) -> dict[str, Any]:
    c = report.content
    els = [_md(f"**最好卖**：{c['best']}\n**正常**：{c['normal']}\n**掉在哪**：{c['dropping']}\n**数据状态**：{c['data_status']}")]
    if c.get("reviews"):
        els.append(_md("**复盘**\n" + "\n".join(f"- {r}" for r in c["reviews"])))
    return _card(f"{c['headline']} · {report.period}", els + _suggestion_blocks(suggestions))


def suggestions(title: str, items: list[Suggestion]) -> dict[str, Any]:
    """单独发的建议卡片：改一改之后的新版本、对话里下达的指令。"""
    return _card(title, _suggestion_blocks(items))


def _suggestion_blocks(items: list[Suggestion]) -> list[dict[str, Any]]:
    els = []
    for x in items:
        els.append(_md(f"**建议 #{x.id}**（置信度 {x.confidence}）\n{x.action}，{x.reason}\n" + "\n".join(f"- {e}" for e in x.evidence)))
        els.append({"tag": "action", "actions": [
            _button("同意", "suggestion", x.id, "approve", x.version, "primary"),
            _button("改一改", "suggestion", x.id, "revise", x.version),
            _button("不同意", "suggestion", x.id, "reject", x.version, "danger"),
        ]})
    return els


def write_confirm(task: Task, before: str, after: str) -> dict[str, Any]:
    what = "将在店铺上新一款商品" if task.executor == "listing" else f"将修改商品 {task.spec.get('goods_id')}"
    return _card("写入确认", [
        _md(f"任务 #{task.id} {what}\n**改前**：{before}\n**改后**：{after}"),
        {"tag": "action", "actions": [
            _button("确认写入", "task", task.id, "confirm", task.version, "primary"),
            _button("取消", "task", task.id, "cancel", task.version, "danger"),
        ]},
    ], template="orange")


def selection_weekly(week: str, cands: list[Candidate], titles: dict[str, str]) -> dict[str, Any]:
    els = []
    for c in cands:
        pp = c.price_plan
        els.append(_md(
            f"**{titles.get(c.offer_id, c.offer_id)}**（{c.score} 分）\n售价 {pp['sell_price_fen'] / 100:.2f} · 成本 {pp['cost_fen'] / 100:.2f}"
            f" · 单件利润 {pp['profit_fen'] / 100:.2f} · 毛利率 {pp['margin']:.0%}\n{c.reason}\n风险：{'；'.join(c.risks) or '无'}"
        ))
        els.append({"tag": "action", "actions": [
            _button("同意上新", "candidate", c.id, "approve", c.version, "primary"),
            _button("改一改", "candidate", c.id, "revise", c.version),
            _button("不要", "candidate", c.id, "reject", c.version, "danger"),
        ]})
    return _card(f"选品周报 · {week}", els or [_md("本周没有达到推荐标准的候选。")], template="green")


TRIAL_LABEL = {"keep": "留款", "optimize": "优化后再观察", "drop": "下架"}


def trial_result(cand: Candidate, title: str, visitors: int, cvr: float, shop_cvr: float, verdict: str, days: int) -> dict[str, Any]:
    body = (f"**{title}** 测款 {days} 天\n访客 {visitors} · 转化率 {cvr:.1%} · 店铺平均 {shop_cvr:.1%}\n"
            f"系统建议：**{TRIAL_LABEL[verdict]}**")
    order = [verdict] + [v for v in ("keep", "optimize", "drop") if v != verdict]
    return _card("测款结论", [_md(body), {"tag": "action", "actions": [
        _button(TRIAL_LABEL[v], "candidate", cand.id, v, cand.version, "primary" if v == verdict else "default") for v in order
    ]}], template="purple")


def need_human(title: str, body: str, resume_job: FetchJob | None = None) -> dict[str, Any]:
    els = [_md(body)]
    if resume_job is not None:
        els.append({"tag": "action", "actions": [
            _button("已处理，恢复采集", "fetch_job", resume_job.id, "resume", resume_job.version, "primary")]})
    return _card(f"需要人工 · {title}", els, template="red")


def resolved(original: dict[str, Any], entity: str, obj_id: int, note: str) -> dict[str, Any]:
    """回调处理完成后，把该实体的按钮换成结果说明；同一卡片里其他实体的按钮保留。"""
    def is_target(e: dict[str, Any]) -> bool:
        return e.get("tag") == "action" and any(
            a.get("value", {}).get("entity") == entity and a.get("value", {}).get("id") == obj_id for a in e["actions"]
        )
    els = [_md(f"✅ {note}") if is_target(e) else e for e in original.get("elements", [])]
    return {**original, "elements": els}
