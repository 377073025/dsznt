"""飞书里的单独任务（07 号文档 17.5 节 P1）：指令提交、查看、取消、转化；完成后私聊通知提交人。

指令格式：第一个词是指令，后面用空格分隔参数，不调用模型解析，结果可预期。
商品可以写名称（「海盐」）或商品 ID；售价按元写。单独任务与 Web 工作台提交的完全一样：
跑在单独通道、不进早报，结果满意再「转为建议 / 候选」。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Callable

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.agents.catalog import CATALOG, IMAGE_MODES
from app.data import queries
from app.interfaces.feishu import cards
from app.interfaces.feishu.notify import send_card
from app.models import AdhocTask, DomainEvent, User
from app.services import Services
from app.workflow import adhoc
from app.workflow.events import subscribe

HELP = (
    "单独任务（跑在单独通道，不影响早报；完成后私聊通知你）：\n"
    "· 诊断 [商品] —— 看数诊断，不写商品看整店\n"
    "· 复盘 商品 —— 改动前后对比\n"
    "· 评估 1688链接 售价 [需求词] —— 单品评估，例如：评估 6543210 29.9 葡萄柚香薰\n"
    "· 找货源 商品名或竞品链接 售价 —— 例如：找货源 葡萄柚香薰蜡烛 29.9\n"
    "· 补货源 商品 —— 按本店商品主图以图搜同款货源\n"
    "· 侦察 类目 [关键词]\n"
    "· 测款 商品 开始日期 —— 例如：测款 g_new 2026-10-01\n"
    "· 文案 商品 要求 —— 例如：文案 海盐 突出无烟、送礼\n"
    "· 修图 商品 要求 / 抠图 商品 / 场景图 商品 要求\n"
    "· 物料 1688链接 售价 需求词 —— 上架物料包草稿\n"
    "· 我的任务 · 取消 编号 · 转化 编号"
)


class CommandError(Exception):
    pass


def _goods(s: Session, word: str) -> str:
    products = queries.products(s)
    for p in products:
        if word in (p.goods_id, p.name):
            return p.goods_id
    hits = [p for p in products if word in p.name]
    if len(hits) == 1:
        return hits[0].goods_id
    if len(hits) > 1:
        raise CommandError(f"「{word}」对应多个商品：{'、'.join(p.name for p in hits)}，请写完整名称")
    raise CommandError(f"找不到商品「{word}」")


def _yuan(word: str) -> int:
    try:
        v = float(word.removesuffix("元"))
    except ValueError:
        raise CommandError(f"「{word}」不是售价，请写成元，例如 29.9") from None
    if v <= 0:
        raise CommandError("售价必须大于 0")
    return round(v * 100)


def _need(args: list[str], n: int, usage: str) -> None:
    if len(args) < n:
        raise CommandError(f"参数不够，格式：{usage}")


@dataclass(frozen=True)
class Command:
    capability: str
    build: Callable[[Session, list[str]], dict[str, Any]]


def _diagnose(s: Session, a: list[str]) -> dict[str, Any]:
    return {"goods_id": _goods(s, a[0])} if a else {}


def _review(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 1, "复盘 商品")
    return {"goods_id": _goods(s, a[0])}


def _evaluate(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 2, "评估 1688链接 售价 [需求词]")
    return {"offer": a[0], "sell_price_fen": _yuan(a[1]), "keyword": " ".join(a[2:])}


def _find_source(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 2, "找货源 商品名或竞品链接 售价")
    if a[0].startswith("http"):
        return {"keyword": "", "competitor_url": a[0], "sell_price_fen": _yuan(a[1])}
    return {"keyword": a[0], "sell_price_fen": _yuan(a[1])}


def _replace_source(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 1, "补货源 商品")
    return {"goods_id": _goods(s, a[0])}


def _scout(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 1, "侦察 类目 [关键词]")
    return {"category": a[0], "keyword": " ".join(a[1:])}


def _trial(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 2, "测款 商品 开始日期")
    try:
        start = date.fromisoformat(a[1])
    except ValueError:
        raise CommandError("开始日期写成 2026-10-01 这样的格式") from None
    try:
        gid = _goods(s, a[0])
    except CommandError:
        gid = a[0]  # 刚上架的测款商品可能还不在商品快照里，按商品 ID 原样使用
    return {"goods_id": gid, "start_date": start.isoformat()}


def _copy(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 2, "文案 商品 要求")
    return {"goods_id": _goods(s, a[0]), "instruction": " ".join(a[1:])}


def _image(mode: str, needs_text: bool) -> Callable[[Session, list[str]], dict[str, Any]]:
    def build(s: Session, a: list[str]) -> dict[str, Any]:
        _need(a, 2 if needs_text else 1, f"{IMAGE_MODES[mode]} 商品" + (" 要求" if needs_text else ""))
        return {"goods_id": _goods(s, a[0]), "instruction": " ".join(a[1:]), "mode": mode}
    return build


def _listing(s: Session, a: list[str]) -> dict[str, Any]:
    _need(a, 3, "物料 1688链接 售价 需求词")
    return {"offer": a[0], "sell_price_fen": _yuan(a[1]), "keyword": " ".join(a[2:])}


COMMANDS: dict[str, Command] = {
    "诊断": Command("ops.diagnose", _diagnose),
    "复盘": Command("ops.review", _review),
    "评估": Command("selection.evaluate_offer", _evaluate),
    "找货源": Command("selection.find_source", _find_source),
    "补货源": Command("selection.find_source", _replace_source),
    "侦察": Command("selection.scout", _scout),
    "测款": Command("selection.trial", _trial),
    "文案": Command("exec.copy", _copy),
    "修图": Command("exec.image", _image("replace_background", True)),
    "抠图": Command("exec.image", _image("remove_background", False)),
    "场景图": Command("exec.image", _image("compose_scene", True)),
    "物料": Command("exec.listing", _listing),
}
_TASK_NO = re.compile(r"#?(\d+)")


def handle(s: Session, services: Services, user: User, text: str) -> str | None:
    """是单独任务指令就处理并返回回复文字；不是返回 None，交给后面的对话处理。"""
    word, *args = text.lstrip("/").split()
    if word in ("我的任务", "任务"):
        return _list(s, user)
    if word in ("取消", "转化") and args and _TASK_NO.fullmatch(args[0]):
        task_id = int(_TASK_NO.fullmatch(args[0]).group(1))
        try:
            if word == "取消":
                t = adhoc.cancel(s, user, task_id)
                return f"单独任务 #{t.id} " + ("已取消。" if t.status == "cancelled" else "正在运行，会在下一轮开始前停止。")
            return f"单独任务 #{task_id} 已转化为 {adhoc.promote(s, services, user, task_id)}，请在卡片上拍板。"
        except (PermissionError, adhoc.AdhocError) as e:
            return str(e)
    cmd = COMMANDS.get(word)
    if cmd is None:
        return None
    try:
        params = cmd.build(s, args)
        task = adhoc.submit(s, user, cmd.capability, params, note=f"飞书：{text}")
    except CommandError as e:
        return f"{e}\n发「帮助」查看指令格式。"
    except PermissionError as e:
        return str(e)
    except ValidationError as e:
        return "参数不对：" + "；".join(f"{'.'.join(map(str, x['loc']))} {x['msg']}" for x in e.errors())
    except adhoc.AdhocError as e:
        return str(e)
    return f"已提交单独任务 #{task.id}（{CATALOG[cmd.capability].title}），完成后私聊通知你。发「取消 {task.id}」可以取消。"


STATUS_LABEL = {"queued": "排队中", "deferred": "已顺延", "running": "运行中", "done": "已完成", "failed": "失败",
                "cancelled": "已取消", "promoted": "已转化"}


def _list(s: Session, user: User) -> str:
    tasks = s.scalars(select(AdhocTask).where(AdhocTask.submitted_by == user.feishu_open_id)
                      .order_by(AdhocTask.id.desc()).limit(10)).all()
    if not tasks:
        return "你还没有提交过单独任务。发「帮助」查看指令。"
    return "最近的单独任务：\n" + "\n".join(
        f"#{t.id} {CATALOG[t.capability].title} · {STATUS_LABEL.get(t.status, t.status)}"
        + (f"（{t.error[:40]}）" if t.error and t.status in ("failed", "deferred") else "") for t in tasks)


# ---------- 结果摘要与完成通知 ----------

def _yuan_str(fen: int) -> str:
    return f"{fen / 100:.2f}"


def summarize(t: AdhocTask) -> str:
    """把结果整理成几行文字，详情在 Web 工作台看。"""
    r = t.result
    if t.status != "done":
        return t.error or STATUS_LABEL.get(t.status, t.status)
    cap = t.capability
    if cap == "ops.diagnose":
        lines = [r["summary"]] + [f"{i + 1}. {x['action']}，{x['reason']}" for i, x in enumerate(r["suggestions"])]
    elif cap == "ops.review":
        lines = [f"{x['day']} 改{x['type']}：转化率 {x['before'].get('cvr_pct')}% → {x['after'].get('cvr_pct')}%，{x['verdict']}"
                 for x in r["items"]] or ["这段时间没有改动记录"]
    elif cap == "selection.evaluate_offer":
        pp = r["price_plan"]
        lines = [f"**{r['title']}**", f"售价 {_yuan_str(pp['sell_price_fen'])} · 单件利润 {_yuan_str(pp['profit_fen'])} · 毛利率 {pp['margin']:.0%}"]
        lines += [f"得分 {r['score']}，{r['reason']}"] if r["passed"] else ["未通过：" + "；".join(r["reject_reasons"])]
    elif cap == "selection.find_source":
        ok = [o for o in r["offers"] if o["passed"]]
        lines = [f"对标「{r['competitor_title']}」，售价 {_yuan_str(r['sell_price_fen'])}，{len(r['offers'])} 个货源中 {len(ok)} 个可用"]
        lines += [f"✅ {o['title']}（进价 {_yuan_str(o['price_fen'])}）" for o in ok[:5]]
    elif cap == "selection.scout":
        lines = [f"{d['keyword']}：热度 {d['heat']:g}，趋势 {d['trend']:+.0%}" for d in r["demands"]] or ["没有找到上升的需求"]
    elif cap == "selection.trial":
        lines = [f"观察 {r['days_observed']} 天：访客 {r['visitors']}，转化率 {r['cvr_pct']}%（店铺 {r['shop_cvr_pct']}%），判断：{r['verdict']}"]
    elif cap == "ceo.ask":
        lines = [r["reply"]]
    elif cap == "exec.copy":
        lines = [f"标题：{r['title']}", "卖点：" + "、".join(r["selling_points"])]
    elif cap == "exec.image":
        lines = [f"{IMAGE_MODES.get(r.get('mode', ''), '修图')}草稿：{r['path']}"]
    elif cap == "exec.listing":
        pkg = r["package"]
        lines = [f"标题：{pkg['title']}", f"售价 {_yuan_str(pkg['price_fen'])}，主图 {len(pkg['main_images'])} 张，物料包：{pkg.get('path', '')}"]
        if r.get("reject_reasons"):
            lines.append("货源未通过硬性过滤：" + "；".join(r["reject_reasons"]))
    else:
        lines = [json.dumps(r, ensure_ascii=False)[:500]]
    return "\n".join(lines)


PROMOTE_LABEL = {"suggestion": "转为建议", "candidate": "转为候选"}


@subscribe("adhoc.done")
@subscribe("adhoc.failed")
def _notify_submitter(s: Session, ev: DomainEvent) -> None:
    from app.services import get_services

    t = s.get(AdhocTask, ev.entity_id)
    if t is None:
        return
    cap = CATALOG[t.capability]
    label = PROMOTE_LABEL[cap.promote_to] if adhoc.can_promote(t) else ""
    card = cards.adhoc_result(t.id, cap.title, t.status, summarize(t), label, t.version)
    send_card(s, get_services(), card, "adhoc_task", t.id, to_user=t.submitted_by)
