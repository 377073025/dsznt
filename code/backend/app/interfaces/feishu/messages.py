"""飞书消息（07 号文档 9.3 节）：文件 → 人工导入；固定指令 → 直接回卡片；
「改一改」后的回复 → CEO 改写；其他文字 → CEO 对话，指令转成建议卡片走审批。

长连接收到消息只入队（feishu.message 作业），这里在 worker 中执行。
"""
from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import ceo
from app.data import queries
from app.data.channels import manual_import
from app.interfaces.feishu import cards
from app.interfaces.feishu.handler import get_or_register
from app.interfaces.feishu.notify import remember_chat, send_card
from app.models import Capability, Candidate, FetchJob, Offer, Report, Shop, Suggestion, User
from app.services import Services
from app.workflow.jobs import JobContext, job_handler
from app.workflow.state_machine import transition

HELP = (
    "可以直接跟我说：\n"
    "· 早报 —— 重发最近一期早报\n"
    "· 选品 —— 看本周推荐\n"
    "· 状态 —— 数据通道与待处理事项\n"
    "· 把导出的表格文件直接发给我 —— 自动导入\n"
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
    command = COMMANDS.get(text)
    if command:
        command(s, services, chat_id)
        return
    pending = _pending_revise(s) if user.role == "owner" else None
    if pending is not None:
        _on_revise(ctx, chat_id, pending, text)
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


def _pending_revise(s: Session) -> Suggestion | None:
    return s.scalar(select(Suggestion).where(Suggestion.status == "revising").order_by(Suggestion.updated_at.desc()))


def _on_revise(ctx: JobContext, chat_id: str, sug: Suggestion, text: str) -> None:
    s, services = ctx.session, ctx.services
    if sug.revise_rounds >= ceo.MAX_REVISE_ROUNDS:
        _reply(services, chat_id, f"建议 #{sug.id} 已经改了 {sug.revise_rounds} 轮，请在 Web 控制台直接编辑后提交。")
        return
    sug = ceo.revise(ctx.runner, s, sug.id, text)
    send_card(s, services, cards.suggestions(f"修改后的建议（第 {sug.revise_rounds} 轮）", [sug]), "suggestion", sug.id)


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

def _cmd_help(s: Session, services: Services, chat_id: str) -> None:
    _reply(services, chat_id, HELP)


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
