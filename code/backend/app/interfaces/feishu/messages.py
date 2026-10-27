"""飞书消息（07 号文档 9.3 节）：文件 → 人工导入；固定指令 → 直接回卡片；
「改一改」后的回复 → CEO 改写；其他文字 → CEO 对话，指令转成建议卡片走审批。

长连接收到消息只入队（feishu.message 作业），这里在 worker 中执行。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import ceo, selection
from app.data import queries
from app.data.channels import manual_import
from app.interfaces.feishu import adhoc_commands, cards
from app.interfaces.feishu.handler import get_or_register, set_role
from app.interfaces.feishu.notify import remember_chat, send_card
from app.models import Capability, Candidate, FetchJob, Offer, Report, Shop, Suggestion, User
from app.services import Services
from app.workflow.jobs import JobContext, job_handler
from app.workflow.state_machine import MODELS, transition

HELP = (
    "可以直接跟我说：\n"
    "· 早报 —— 重发最近一期早报\n"
    "· 选品 —— 看本周推荐\n"
    "· 状态 —— 数据通道与待处理事项\n"
    "· 把导出的表格文件直接发给我 —— 自动导入\n"
    "· 设为运营 @某人 / 设为查看 @某人 —— 调整角色（店主）\n"
    "· 其他问题直接问，例如「海盐最近怎么样」"
)
_MENTION = re.compile(r"@_user_\d+\s*")


def _reply(services: Services, chat_id: str, text: str) -> None:
    if services.feishu is not None:
        services.feishu.send_text(chat_id, text)


def handle_message(ctx: JobContext, p: dict[str, Any]) -> None:
    s, services = ctx.session, ctx.services
    user = get_or_register(s, p["open_id"])
    chat_id = p["chat_id"]
    if p.get("chat_type") == "group" and remember_chat(s, chat_id):
        _reply(services, chat_id, "已把这个群设为店铺早报和提醒的推送群。")
    if p["msg_type"] == "file":
        _on_file(s, services, user, chat_id, p)
        return
    text = _MENTION.sub("", p.get("text", "")).strip()
    if not text:
        return
    reply = _role_command(s, user, p.get("text", ""), p.get("mentions") or [])
    if reply is not None:
        _reply(services, chat_id, reply)
        return
    command = COMMANDS.get(text)
    if command:
        command(s, services, chat_id)
        return
    reply = adhoc_commands.handle(s, services, user, text)
    if reply is not None:
        _reply(services, chat_id, reply)
        return
    pending = _pending_revise(s) if user.role == "owner" else None
    if pending is not None:
        refused = revise(ctx, pending, text)
        if refused:
            _reply(services, chat_id, refused)
        return
    _on_chat(ctx, user, chat_id, text)


def _on_file(s: Session, services: Services, user: User, chat_id: str, p: dict[str, Any]) -> None:
    if user.role not in ("owner", "operator"):
        _reply(services, chat_id, "你还没有上传数据的权限，请联系店主在「用户与权限」里设置。")
        return
    path = services.feishu.download_file(p["message_id"], p["file_key"])
    rec = manual_import.import_file(s, s.scalar(select(Shop).order_by(Shop.id)), path, user.feishu_open_id)
    if rec.status == "imported":
        _reply(services, chat_id, f"已导入 {rec.rows} 行（{rec.item}），相关分析会自动重新开始。")
    elif rec.status == "unrecognized":
        _reply(services, chat_id, "这份文件我不认识。是哪个页面导出的？目前支持：拼多多后台「商品数据」导出。")
    else:
        rows = "；".join(f"第 {e['row']} 行：{e['error']}" for e in rec.error_rows[:5])
        _reply(services, chat_id, f"文件里有数据不对，没有导入：{rows}")


def _pending_revise(s: Session) -> Suggestion | Candidate | None:
    """最近一次点了「改一改」的建议或候选：店主接下来的回复就是修改意见。"""
    found = [x for x in (
        s.scalar(select(Suggestion).where(Suggestion.status == "revising").order_by(Suggestion.updated_at.desc())),
        s.scalar(select(Candidate).where(Candidate.status == "revising").order_by(Candidate.updated_at.desc())),
    ) if x is not None]
    return max(found, key=lambda x: x.updated_at) if found else None


def revise(ctx: JobContext, obj: Suggestion | Candidate, text: str) -> str:
    """按修改意见改写并发新卡片；超过轮数时返回提示文字（不改）。飞书回复与 Web 的「改一改」都走这里。"""
    s, services = ctx.session, ctx.services
    if obj.revise_rounds >= ceo.MAX_REVISE_ROUNDS:
        what = "建议" if isinstance(obj, Suggestion) else "候选"
        return f"{what} #{obj.id} 已经改了 {obj.revise_rounds} 轮，请在 Web 控制台直接处理。"
    if isinstance(obj, Suggestion):
        sug = ceo.revise(ctx.runner, s, obj.id, text)
        send_card(s, services, cards.suggestions(f"修改后的建议（第 {sug.revise_rounds} 轮）", [sug]), "suggestion", sug.id)
    else:
        cand = selection.revise_candidate(ctx.runner, s, services, obj.id, text)
        title = s.get(Offer, cand.offer_id).title
        send_card(s, services, cards.selection_weekly(f"修改后的候选（第 {cand.revise_rounds} 轮）", [cand],
                                                      {cand.offer_id: title}), "candidate", cand.id)
    return ""


@job_handler("revise.run")
def _revise_run(ctx: JobContext, p: dict[str, Any]) -> None:
    """Web 上提交的「改一改」：先由 card.apply 把状态改成 revising，再按意见改写。"""
    obj = ctx.session.get(MODELS[p["entity"]], p["id"])
    if obj is None or obj.status != "revising":
        raise RuntimeError(f"{p['entity']} #{p['id']} 不在待修改状态")
    refused = revise(ctx, obj, p["feedback"])
    if refused:
        raise RuntimeError(refused)


def _on_chat(ctx: JobContext, user: User, chat_id: str, text: str) -> None:
    s, services = ctx.session, ctx.services
    out = ceo.chat(ctx.runner, s, text)
    _reply(services, chat_id, out.reply)
    if out.instruction and user.role == "owner":
        shop = s.scalar(select(Shop).order_by(Shop.id))
        sug = Suggestion(shop_id=shop.id, day=queries.business_day(s), type="other", action=out.instruction,
                         reason="店主在飞书中提出", confidence="high", needs_write=True)
        s.add(sug)
        s.flush()
        transition(s, "suggestion", sug.id, "report", ceo.CEO, {"from_chat": True})
        send_card(s, services, cards.suggestions("请确认这条指令", [sug]), "suggestion", sug.id)


# ---------- 固定指令（不调用模型） ----------

_ROLE_CMD = re.compile(r"^设为(运营|查看)")
_ROLE_WORDS = {"运营": "operator", "查看": "viewer"}


def _role_command(s: Session, user: User, raw: str, mentions: list[dict[str, Any]]) -> str | None:
    """「设为运营 @某人」「设为查看 @某人」：只取指令后面 @ 的人（群里 @ 机器人的那个不算）。"""
    m = _ROLE_CMD.match(_MENTION.sub("", raw).strip())
    if m is None:
        return None
    if user.role != "owner":
        return "只有店主可以调整角色。"
    at = raw.find(m.group(0)) + len(m.group(0))
    targets = [x for x in mentions if raw.find(x["key"], at) >= 0]
    if not targets:
        return f"请在指令后 @ 要调整的人，例如：设为{m.group(1)} @张三"
    done, refused = [], []
    for x in targets:
        target = get_or_register(s, x["open_id"])
        try:
            set_role(s, user, target, _ROLE_WORDS[m.group(1)], name=target.name or x.get("name", ""))
            done.append(target.name or x["open_id"])
        except ValueError as e:
            refused.append(f"{target.name or x['open_id']}：{e}")
    return "；".join(([f"已把 {'、'.join(done)} 设为{m.group(1)}"] if done else []) + refused) + "。"


def _cmd_help(s: Session, services: Services, chat_id: str) -> None:
    _reply(services, chat_id, HELP + "\n\n" + adhoc_commands.HELP)


def _cmd_report(s: Session, services: Services, chat_id: str) -> None:
    r = s.scalar(select(Report).where(Report.kind == "daily").order_by(Report.period.desc()))
    if r is None:
        _reply(services, chat_id, "还没有早报。")
        return
    sugs = [x for x in (s.get(Suggestion, i) for i in r.content["suggestion_ids"]) if x and x.status == "reported"]
    send_card(s, services, cards.daily_report(r, sugs), "report", r.id, also=[("suggestion", x.id) for x in sugs])


def _cmd_selection(s: Session, services: Services, chat_id: str) -> None:
    cands = list(s.scalars(select(Candidate).where(Candidate.status == "recommended").order_by(Candidate.score.desc())))
    titles = {o.id: o.title for o in s.scalars(select(Offer).where(Offer.id.in_([c.offer_id for c in cands])))}
    send_card(s, services, cards.selection_weekly("当前待拍板", cands, titles), "selection_week", 0,
              also=[("candidate", c.id) for c in cands])


_STATUS_LABEL = {"healthy": "正常", "degraded": "异常", "blocked": "暂停", "configured": "待检查", "not_configured": "未接入"}


def _cmd_status(s: Session, services: Services, chat_id: str) -> None:
    caps = s.scalars(select(Capability)).all()
    waiting = s.scalars(select(FetchJob).where(FetchJob.status.in_(("manual_requested", "waiting_human", "circuit_open")))).all()
    pending = s.scalars(select(Suggestion).where(Suggestion.status.in_(("reported", "revising")))).all()
    lines = ["数据通道：" + "；".join(f"{c.key} {_STATUS_LABEL.get(c.status, c.status)}" for c in caps)]
    lines.append(f"等待人工的采集：{len(waiting)} 项" + ("（" + "、".join(f"{j.day} {j.item}" for j in waiting) + "）" if waiting else ""))
    lines.append(f"待拍板的建议：{len(pending)} 条")
    _reply(services, chat_id, "\n".join(lines))


COMMANDS = {"帮助": _cmd_help, "help": _cmd_help, "早报": _cmd_report, "选品": _cmd_selection, "状态": _cmd_status}


@job_handler("feishu.message")
def _feishu_message(ctx: JobContext, p: dict[str, Any]) -> None:
    handle_message(ctx, p)
