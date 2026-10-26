"""各智能体的模拟应答器。只使用 context / facts 中已有的数字，保证能通过数字一致性校验。"""
from __future__ import annotations

import json
from typing import Any

from app.adapters.llm.base import LLMRequest
from app.adapters.llm.fake import tool_call_result


def _f(v: float) -> str:
    return str(int(v)) if v == int(v) else f"{v:g}"


def _last_tool_result(req: LLMRequest) -> dict[str, Any] | None:
    last = req.messages[-1]
    if isinstance(last.get("content"), list):
        for b in last["content"]:
            if isinstance(b, dict) and b.get("type") == "tool_result":
                return b
    return None


def ops_diagnose(req: LLMRequest) -> dict:
    facts, goods = req.context["facts"], req.context["goods"]
    suggestions = []
    for g in goods:
        k = g["goods_id"]
        img_events = [e for e in g["change_events"] if e["type"] == "main_image"]
        if not g["anomaly"]:
            continue
        cur, prev = facts[f"{k}.cvr_cur_pct"], facts[f"{k}.cvr_prev_pct"]
        if img_events:
            e = img_events[-1]
            suggestions.append({
                "goods_id": k, "type": "main_image",
                "action": f"建议把{g['name']}的主图换回原来的场景图",
                "reason": f"因为{e['day']}主图换成白底图后，近{_f(facts['window.days'])}天转化率从{_f(prev)}%降到{_f(cur)}%",
                "evidence": [f"近{_f(facts['window.days'])}天访客{_f(facts[f'{k}.visitors_cur'])}，下单{_f(facts[f'{k}.orders_cur'])}",
                             f"{e['day']}主图改动：{e['before']} → {e['after']}"],
                "confidence": "high", "needs_write": True,
            })
        else:
            suggestions.append({
                "goods_id": k, "type": "other",
                "action": f"建议观察{g['name']}的转化率变化",
                "reason": f"近{_f(facts['window.days'])}天转化率{_f(cur)}%，低于前{_f(facts['window.days'])}天的{_f(prev)}%，暂未找到对应改动",
                "evidence": [], "confidence": "low", "needs_write": False,
            })
    return {"summary": "已检查全部商品，需要关注的商品见建议。" if suggestions else "各商品指标正常。", "suggestions": suggestions}


def ceo_daily(req: LLMRequest) -> dict:
    g, sugs = req.context["groups"], req.context["suggestions"]
    return {
        "headline": "今日店铺早报",
        "best": "、".join(g["best"]) or "无",
        "normal": "、".join(g["normal"]) or "无",
        "dropping": "、".join(g["dropping"]) or "无",
        "suggestion_ids": [x["id"] for x in sugs],
        "data_status": req.context["data_status"],
    }


def ceo_dispatch(req: LLMRequest):
    res = _last_tool_result(req)
    sug = req.context["suggestion"]
    if res is None:
        executor = "image" if sug["type"] == "main_image" else "copy"
        return tool_call_result("create_task", {
            "executor": executor, "goods_id": sug["goods_id"], "change": sug["type"],
            "instruction": "主图换回改动前的场景图" if executor == "image" else "按建议改写标题",
            "needs_write": sug["needs_write"],
            "checklist": ["主图已换回场景图", "商品其他信息未被改动"] if executor == "image" else ["标题无违禁词", "标题长度合规"],
        })
    task_id = json.loads(res["content"])["task_id"]
    return {"task_ids": [task_id], "note": "已派活"}


def ceo_accept(req: LLMRequest):
    res = _last_tool_result(req)
    if res is None:
        return tool_call_result("get_task_output", {"task_id": req.context["task_id"]})
    out = req.context["output"]
    ok = bool(out.get("patch") or out.get("package")) and bool(out.get("write_ref") or not req.context["needs_write"])
    return {"passed": ok, "items": [{"item": c, "ok": ok, "note": "已核对产物"} for c in req.context["checklist"]],
            "comment": "验收通过" if ok else "产物缺失"}


def selection_same_product(req: LLMRequest) -> dict:
    comp, offer = req.context["competitor"], req.context["offer"]
    same = "葡萄柚" in comp and "葡萄柚" in offer
    return {"same_product": same, "confidence": 0.92 if same else 0.3,
            "evidence": ["品名与规格一致"] if same else [], "concerns": [] if same else ["品名不一致"]}


def selection_recommend(req: LLMRequest) -> dict:
    f = req.context["facts"]
    return {
        "reason": (f"建议上新{req.context['keyword']}，因为相关热词热度上升{_f(f['demand.trend_pct'])}%，"
                   f"按售价{_f(f['plan.sell_price_yuan'])}元估算单件利润{_f(f['plan.profit_yuan'])}元、毛利率{_f(f['plan.margin_pct'])}%，"
                   f"供应商经营{_f(f['supplier.years'])}年、48小时发货率{_f(f['supplier.ship_48h_pct'])}%。"),
        "risks": ["该价格带竞品较多，需要差异化主图"],
    }


def exec_copy(req: LLMRequest) -> dict:
    return {"title": f"{req.context['name']} 家用卧室 持久留香", "selling_points": ["大豆蜡", "无烟"]}


def ceo_revise(req: LLMRequest) -> dict:
    sug, feedback = req.context["suggestion"], req.context["feedback"]
    return {"action": f"{sug['action']}（已按店主意见调整：{feedback}）", "reason": sug["reason"],
            "evidence": sug["evidence"], "needs_write": sug["needs_write"]}


def ceo_chat(req: LLMRequest) -> dict:
    text, facts = req.context["text"], req.context["facts"]
    if text.startswith(("帮我", "把")):
        return {"reply": "收到，我把这条指令整理成建议，请在卡片上确认。", "instruction": text}
    for g in req.context["goods"]:
        if g["name"][:2] in text:
            k = g["goods_id"]
            return {"reply": f"{g['name']}近{_f(facts['window.days'])}天转化率{_f(facts[f'{k}.cvr_cur_pct'])}%，"
                             f"前{_f(facts['window.days'])}天{_f(facts[f'{k}.cvr_prev_pct'])}%。", "instruction": ""}
    return {"reply": "这个问题我暂时回答不了，可以换个说法，或者发「帮助」看看我能做什么。", "instruction": ""}


DEFAULT_RESPONDERS = {
    "ceo.revise": ceo_revise,
    "ceo.chat": ceo_chat,
    "ops.diagnose": ops_diagnose,
    "ceo.daily": ceo_daily,
    "ceo.dispatch": ceo_dispatch,
    "ceo.accept": ceo_accept,
    "selection.same_product": selection_same_product,
    "selection.recommend": selection_recommend,
    "exec.copy": exec_copy,
}
