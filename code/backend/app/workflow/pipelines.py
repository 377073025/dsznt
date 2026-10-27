"""编排（07 号文档 8.2 节）：作业处理器 + 领域事件订阅。

每日：采集 → data.ready 全部到齐 → 快照对比、运营分析、CEO 早报 → 飞书。
拍板后：approved → 派活 → 执行 → （写入确认）→ 写店铺 → 验收 → 完成。
"""
from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents import ceo, executors, ops, review, selection
from app.config import get_settings
from app.data import queries, router, snapshot
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import send_card
from app.models import Candidate, DomainEvent, FetchJob, Job, LlmBatch, Offer, Product, Shop, Suggestion, Task, TestRun
from app.workflow import on_sale
from app.workflow.events import subscribe
from app.workflow.jobs import JobContext, enqueue, job_handler
from app.workflow.state_machine import SYSTEM, transition

log = logging.getLogger(__name__)
DAILY_ITEMS = ["goods_snapshot", "goods_daily_traffic", "order_stats"]


def _shop(s: Session) -> Shop:
    shop = s.scalar(select(Shop).order_by(Shop.id))
    assert shop is not None, "尚未初始化店铺（scripts/init_db.py）"
    return shop


# ---------- 作业 ----------

@job_handler("daily.fetch")
def daily_fetch(ctx: JobContext, p: dict[str, Any]) -> None:
    day = date.fromisoformat(p["day"])
    shop = _shop(ctx.session)
    for item in DAILY_ITEMS:
        router.fetch(ctx.session, ctx.services, shop, item, day, force=p.get("force", False))


@job_handler("daily.analyze")
def daily_analyze(ctx: JobContext, p: dict[str, Any]) -> None:
    s, day = ctx.session, date.fromisoformat(p["day"])
    shop = _shop(s)
    snapshot.detect_changes(s, day)
    jobs = {j.item: j.status for j in s.scalars(select(FetchJob).where(FetchJob.day == day))}
    missing = [i for i in DAILY_ITEMS if jobs.get(i) != router.SUCCESS]
    # 缺少分款流量数据时不做诊断，避免在残缺数据上下结论
    if "goods_daily_traffic" not in missing:
        batch_conf = get_settings().raw["llm_batch"]
        batch = s.get(LlmBatch, p["batch_id"]) if p.get("batch_id") else None
        if batch is None and batch_conf["enabled"] and ops.SPEC.name in batch_conf["agents"]:
            # 诊断先提交 Batch（半价），结果回来后由 llm.batch_poll 重新排入本作业；超时则改走实时调用
            b = ctx.gateway.submit_batch(s, ops.SPEC.name, {"ops": ops.diagnose_request(ctx.runner, s, day)},
                                         {"type": "daily.analyze", "payload": dict(p)},
                                         timedelta(minutes=batch_conf["max_wait_minutes"]))
            enqueue(s, "llm.batch_poll", {"batch_id": b.id})
            return
        ops.run(ctx.runner, s, shop, day, first=ctx.gateway.batch_result(batch, "ops"))
    reviews = [review.describe(s, rv) for rv in review.run_due_reviews(s, day)]
    status = "全部数据已就绪" if not missing else f"缺少 {'、'.join(missing)}，相关分析已跳过"
    report = ceo.daily_report(ctx.runner, s, day, status, reviews)
    sugs = [s.get(Suggestion, i) for i in report.content["suggestion_ids"]]
    report.feishu_message_id = send_card(
        s, ctx.services, cards.daily_report(report, sugs), "report", report.id, also=[("suggestion", x.id) for x in sugs],
    ) or ""
    for tr, shop_cvr in review.run_due_trials(s, day):
        cand = s.get(Candidate, tr.candidate_id)
        title = s.get(Offer, cand.offer_id).title
        send_card(s, ctx.services, cards.trial_result(cand, title, tr.visitors, tr.cvr, shop_cvr, tr.verdict, tr.days,
                                                      tr.trial_round), "candidate", cand.id)


@job_handler("ceo.dispatch")
def ceo_dispatch(ctx: JobContext, p: dict[str, Any]) -> None:
    for t in ceo.dispatch(ctx.runner, ctx.session, p["suggestion_id"]):
        enqueue(ctx.session, "task.execute", {"task_id": t.id}, dedupe_key=f"task.execute:{t.id}:0")


@job_handler("task.execute")
def task_execute(ctx: JobContext, p: dict[str, Any]) -> None:
    executors.execute(ctx.runner, ctx.session, ctx.services, p["task_id"])


@job_handler("task.write")
def task_write(ctx: JobContext, p: dict[str, Any]) -> None:
    executors.write(ctx.session, ctx.services, p["task_id"])


@job_handler("ceo.accept")
def ceo_accept(ctx: JobContext, p: dict[str, Any]) -> None:
    ceo.accept(ctx.runner, ctx.session, p["task_id"])


@job_handler("selection.weekly")
def selection_weekly(ctx: JobContext, p: dict[str, Any]) -> None:
    s = ctx.session
    cands = selection.run_weekly(ctx.runner, s, ctx.services, p["week"], p["category"])
    titles = {o.id: o.title for o in s.scalars(select(Offer).where(Offer.id.in_([c.offer_id for c in cands])))}
    send_card(s, ctx.services, cards.selection_weekly(p["week"], cands, titles), "selection_week", 0,
              also=[("candidate", c.id) for c in cands])


# ---------- 事件订阅 ----------

@subscribe("data.ready")
def _on_data_ready(s: Session, ev: DomainEvent) -> None:
    day = date.fromisoformat(ev.payload["day"])
    if ev.payload["item"] == "goods_snapshot":
        snapshot.detect_changes(s, day)  # 每天的快照到齐就和前一天对比，补采的历史也一样
        on_sale.backfill_listed_ids(s, day)  # 新上架的测款商品出现在快照里 → 回填拼多多商品 ID
    # 只分析有每日采集作业的那天；补采历史数据不触发分析
    scheduled = any(j.payload.get("day") == day.isoformat() for j in s.scalars(select(Job).where(Job.type == "daily.fetch")))
    if scheduled and router.all_ready(s, DAILY_ITEMS, day):
        enqueue(s, "daily.analyze", {"day": day.isoformat()}, dedupe_key=f"daily.analyze:{day.isoformat()}")


@subscribe("fetch.missing")
def _on_missing(s: Session, ev: DomainEvent) -> None:
    """数据到截止时间仍缺失：其余数据项都已结束（成功或缺失）时，带着缺失出早报。"""
    day = date.fromisoformat(ev.payload["day"])
    scheduled = any(j.payload.get("day") == day.isoformat() for j in s.scalars(select(Job).where(Job.type == "daily.fetch")))
    jobs = s.scalars(select(FetchJob).where(FetchJob.day == day, FetchJob.item.in_(DAILY_ITEMS))).all()
    if scheduled and len(jobs) == len(DAILY_ITEMS) and all(j.status in (router.SUCCESS, router.MISSING) for j in jobs):
        enqueue(s, "daily.analyze", {"day": day.isoformat()}, dedupe_key=f"daily.analyze:{day.isoformat()}")


@subscribe("suggestion.approved")
def _on_suggestion_approved(s: Session, ev: DomainEvent) -> None:
    enqueue(s, "ceo.dispatch", {"suggestion_id": ev.entity_id}, dedupe_key=f"ceo.dispatch:{ev.entity_id}")


@subscribe("candidate.approved")
def _on_candidate_approved(s: Session, ev: DomainEvent) -> None:
    enqueue(s, "selection.prepare", {"candidate_id": ev.entity_id}, dedupe_key=f"selection.prepare:{ev.entity_id}")


@job_handler("selection.prepare")
def selection_prepare(ctx: JobContext, p: dict[str, Any]) -> None:
    """同意上新 → 生成上架物料任务。上新一定要写店铺，所以 needs_write 固定为 True。"""
    s = ctx.session
    cand = s.get(Candidate, p["candidate_id"])
    transition(s, "candidate", cand.id, "prepare", selection.SELECTOR)
    task = Task(ref_type="candidate", ref_id=cand.id, executor="listing", needs_write=True,
                spec={"offer_id": cand.offer_id, "change": "listing", "instruction": "生成上架物料包并推送铺货工具"},
                checklist=["标题无违禁词", "主图至少 1 张", "售价与选品方案一致", "附货源链接"])
    s.add(task)
    s.flush()
    enqueue(s, "task.execute", {"task_id": task.id}, dedupe_key=f"task.execute:{task.id}:0")


@subscribe("task.pending")
def _on_task_rework(s: Session, ev: DomainEvent) -> None:
    task = s.get(Task, ev.entity_id)
    enqueue(s, "task.execute", {"task_id": task.id}, dedupe_key=f"task.execute:{task.id}:{task.rework_count}")


@subscribe("task.awaiting_write_confirm")
def _on_awaiting_write(s: Session, ev: DomainEvent) -> None:
    from app.services import get_services

    task = s.get(Task, ev.entity_id)
    if task.executor == "listing":
        pkg = task.output["package"]
        before = "（新商品）"
        after = f"标题：{pkg['title']}；售价：{pkg['price_fen'] / 100:.2f}；主图 {len(pkg['main_images'])} 张；物料包：{pkg['path']}"
    else:
        prod = s.scalar(select(Product).where(Product.goods_id == task.spec.get("goods_id")))
        patch = task.output.get("patch", {})
        before = "；".join(f"{k}: {getattr(prod, 'main_image_hash' if k == 'main_image' else k, '')}" for k in patch) if prod else ""
        after = "；".join(f"{k}: {v}" for k, v in patch.items())
    send_card(s, get_services(), cards.write_confirm(task, before, after), "task", task.id)


@subscribe("task.writing")
def _on_writing(s: Session, ev: DomainEvent) -> None:
    enqueue(s, "task.write", {"task_id": ev.entity_id}, dedupe_key=f"task.write:{ev.entity_id}:{ev.id}")


@subscribe("task.submitted")
def _on_submitted(s: Session, ev: DomainEvent) -> None:
    enqueue(s, "ceo.accept", {"task_id": ev.entity_id}, dedupe_key=f"ceo.accept:{ev.entity_id}:{ev.id}")


@subscribe("task.accepted")
def _on_accepted(s: Session, ev: DomainEvent) -> None:
    task = s.get(Task, ev.entity_id)
    if task.ref_type == "candidate":
        # 铺货完成并验收 → 上架 → 开始 14 天测款。
        # 商品在拼多多的 ID 先记铺货工具回执，之后由商品快照按标题匹配回填（on_sale.backfill_listed_ids）
        cand = s.get(Candidate, task.ref_id)
        transition(s, "candidate", cand.id, "list", SYSTEM)
        transition(s, "candidate", cand.id, "start_test", SYSTEM)
        s.add(TestRun(candidate_id=cand.id, goods_id=task.output.get("write_ref", ""), start_date=queries.business_day(s),
                      days=get_settings().rules["selection"]["test_run"]["days"]))
        return
    if task.ref_type != "suggestion":
        return
    siblings = s.scalars(select(Task).where(Task.ref_type == "suggestion", Task.ref_id == task.ref_id)).all()
    if all(t.status == "accepted" for t in siblings):
        transition(s, "suggestion", task.ref_id, "complete", SYSTEM)


def _need_human(s: Session, title: str, body: str, entity: str, entity_id: int, resumable: bool = False) -> None:
    from app.services import get_services

    job = s.get(FetchJob, entity_id) if resumable else None
    send_card(s, get_services(), cards.need_human(title, body, job), entity, entity_id)


@subscribe("fetch.manual_requested")
def _on_manual(s: Session, ev: DomainEvent) -> None:
    p = ev.payload
    _need_human(s, "请导出数据", f"{p['day']} 的 {p['item']} 自动获取失败，请按指引导出「{p['template']}」后直接发给我。\n"
                + _guide_text(p["guide"]), "fetch_job", ev.entity_id)


def _guide_text(guide: str) -> str:
    """导出指引正文（去掉标题行）；文件不存在时只给路径。"""
    from app.config import CONFIG_DIR

    path = CONFIG_DIR / guide
    if not guide or not path.is_file():
        return f"指引：{guide}"
    lines = path.read_text(encoding="utf-8").splitlines()
    return "\n".join(x for x in lines if not x.startswith("# ")).strip()


@subscribe("fetch.waiting_human")
def _on_waiting(s: Session, ev: DomainEvent) -> None:
    p = ev.payload
    what = "出现验证码，请在专用浏览器里完成验证" if p["kind"] == "captcha" else "登录已失效，请在专用浏览器里重新扫码登录"
    _need_human(s, "平台暂停", f"{p['capability']}：{what}。处理后点「恢复采集」。", "fetch_job", ev.entity_id, resumable=True)


@subscribe("fetch.circuit_open")
def _on_circuit(s: Session, ev: DomainEvent) -> None:
    _need_human(s, "已熔断", f"{ev.payload['capability']} 出现访问受限提示，已暂停 24 小时以上，需要店主确认后恢复。",
                "fetch_job", ev.entity_id, resumable=True)


@subscribe("task.escalated")
@subscribe("task.failed")
def _on_task_problem(s: Session, ev: DomainEvent) -> None:
    _need_human(s, "任务异常", f"任务 #{ev.entity_id} {ev.type}：{ev.payload.get('comment') or ev.payload.get('error', '')}",
                "task", ev.entity_id)


# ---------- 测款「优化」：交运营智能体诊断，再观察 ----------

@subscribe("candidate.optimizing")
def _on_optimizing(s: Session, ev: DomainEvent) -> None:
    enqueue(s, "selection.optimize", {"candidate_id": ev.entity_id}, dedupe_key=f"selection.optimize:{ev.entity_id}")


@job_handler("selection.optimize")
def selection_optimize(ctx: JobContext, p: dict[str, Any]) -> None:
    """店主确认「优化后再观察」：运营智能体只诊断这一款，给出的建议照常走拍板；同时开始 optimize_days 天的再观察。"""
    s = ctx.session
    cand = s.get(Candidate, p["candidate_id"])
    tr = s.scalar(select(TestRun).where(TestRun.candidate_id == cand.id).order_by(TestRun.id.desc()))
    cfg = get_settings().rules
    day = queries.business_day(s)
    out = ops.diagnose(ctx.runner, s, day, goods_id=tr.goods_id)
    shop, title = _shop(s), s.get(Offer, cand.offer_id).title
    sugs = []
    for x in out.suggestions[: cfg["ops"]["max_suggestions_per_report"]]:
        sug = Suggestion(shop_id=shop.id, goods_id=x.goods_id, day=day, type=x.type, action=x.action,
                         reason=f"{x.reason}（测款优化：候选 #{cand.id}）", evidence=x.evidence, confidence=x.confidence,
                         needs_write=x.needs_write)
        s.add(sug)
        s.flush()
        transition(s, "suggestion", sug.id, "report", ceo.CEO, {"candidate_id": cand.id})
        sugs.append(sug)
    days = cfg["selection"]["test_run"]["optimize_days"]
    s.add(TestRun(candidate_id=cand.id, goods_id=tr.goods_id, start_date=day + timedelta(days=1), days=days,
                  trial_round=tr.trial_round + 1))
    transition(s, "candidate", cand.id, "retest", SYSTEM)
    if sugs:
        send_card(s, ctx.services, cards.suggestions(f"测款优化 · {title}（拍板后执行，再观察 {days} 天）", sugs),
                  "suggestion", sugs[0].id, also=[("suggestion", x.id) for x in sugs[1:]])
    else:
        send_card(s, ctx.services, cards.notice(f"测款优化 · {title}",
                                                f"运营智能体没有发现明确的问题（{out.summary}），继续观察 {days} 天后再给结论。",
                                                "purple"), "candidate", cand.id)
