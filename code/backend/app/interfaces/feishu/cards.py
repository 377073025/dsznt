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


def trial_result(cand: Candidate, title: str, visitors: int, cvr: float, shop_cvr: float, verdict: str, days: int,
                 trial_round: int = 1) -> dict[str, Any]:
    what = f"优化后再观察 {days} 天" if trial_round > 1 else f"测款 {days} 天"
    body = (f"**{title}** {what}\n访客 {visitors} · 转化率 {cvr:.1%} · 店铺平均 {shop_cvr:.1%}\n"
            f"系统建议：**{TRIAL_LABEL[verdict]}**")
    options = ("keep", "drop") if trial_round > 1 else ("keep", "optimize", "drop")  # 只优化一轮
    order = [verdict] + [v for v in options if v != verdict]
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


def notice(title: str, body: str, template: str = "orange") -> dict[str, Any]:
    """无按钮的通知：预算提醒、备份失败等。"""
    return _card(title, [_md(body)], template=template)


def system_weekly(period: str, c: dict[str, Any]) -> dict[str, Any]:
    f, cost = c["fetch"], c["cost"]
    lines = [
        f"**数据采集**：{f['total']} 项，成功 {f['success']}（{f['success_rate']:.0%}），人工导入 {f['manual']}，缺失 {f['missing']}",
        f"**AI 认路**：{c['repairs']} 次；**风控事件**：{c['risk_total']} 次"
        + (f"（{'、'.join(f'{k} {v}' for k, v in c['risks'].items())}）" if c["risks"] else ""),
        f"**作业失败**：{c['jobs_failed']} 个；**智能体运行失败**：{c['runs_failed']} 次",
        f"**LLM 成本**：主流程 ¥{cost['main']:.2f}，单独任务 ¥{cost['adhoc']:.2f}；本月累计 ¥{cost['month']:.2f} / 预算 ¥{cost['monthly_budget']:.0f}",
    ]
    if cost["top_agents"]:
        lines.append("花费最多：" + "、".join(f"{a} ¥{v:.2f}" for a, v in cost["top_agents"]))
    apps = c["applications"]
    lines.append(f"**申请事项**：已完成 {apps['done']} / {apps['total']}"
                 + ("；未完成：" + "、".join(apps["pending"]) if apps["pending"] else ""))
    return _card(f"周度系统报告 · {period}", [_md("\n".join(lines))], template="grey")


def adhoc_result(task_id: int, title: str, status: str, summary: str, promote_label: str = "",
                 version: int = 0) -> dict[str, Any]:
    """单独任务完成通知（私聊提交人）。结果可转化时带「转为建议 / 候选」按钮。"""
    head = {"done": "已完成", "failed": "失败", "cancelled": "已取消"}.get(status, status)
    els = [_md(summary)]
    if promote_label:
        els.append({"tag": "action", "actions": [
            _button(promote_label, "adhoc_task", task_id, "promote", version, "primary")]})
    return _card(f"单独任务 #{task_id} · {title} · {head}", els,
                 template={"done": "green", "failed": "red"}.get(status, "grey"))
