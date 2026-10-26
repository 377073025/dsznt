"""写工具。internal 级写系统内部；shop 级写店铺，执行前再校验一次审批状态。"""
from __future__ import annotations

from pydantic import BaseModel
from sqlalchemy import select

from app.adapters.types import GoodsInfo, GoodsPatch, ListingPackage
from app.data import queries
from app.models import ChangeEvent, Product, Task, User
from app.tools.registry import ToolContext, tool
from app.workflow.events import audit


class WriteNotAllowed(Exception):
    pass


def ensure_write_allowed(ctx: ToolContext, task_id: int) -> Task:
    """写店铺的最后一道检查：任务处于 writing，且写入确认来自 owner。"""
    task = ctx.session.get(Task, task_id)
    if task is None or task.status != "writing" or not task.write_confirmed_by:
        audit(ctx.session, f"agent:{ctx.agent}", "write.denied", f"task:{task_id}", {"status": getattr(task, "status", None)})
        raise WriteNotAllowed(f"任务 #{task_id} 未经写入确认，拒绝写店铺")
    user = ctx.session.scalar(select(User).where(User.feishu_open_id == task.write_confirmed_by))
    if user is None or user.role != "owner":
        raise WriteNotAllowed(f"任务 #{task_id} 的写入确认人不是店主")
    return task


class CreateTaskArgs(BaseModel):
    executor: str  # copy / image / listing
    goods_id: str
    change: str  # main_image / title / price / listing
    instruction: str
    needs_write: bool
    checklist: list[str]


@tool("create_task", "为当前已批准的建议创建一个执行任务，并写明验收清单。", CreateTaskArgs, "internal")
def create_task(ctx: ToolContext, a: CreateTaskArgs) -> dict:
    ref = ctx.session.info.get("dispatch_ref")
    if not ref:
        return {"error": "当前没有正在派活的建议"}
    task = Task(
        ref_type=ref[0], ref_id=ref[1], executor=a.executor,
        spec={"goods_id": a.goods_id, "change": a.change, "instruction": a.instruction},
        needs_write=a.needs_write, checklist=a.checklist,
    )
    ctx.session.add(task)
    ctx.session.flush()
    return {"task_id": task.id}


class TaskIdArgs(BaseModel):
    task_id: int


@tool("pdd_update_goods", "把任务产物写入拼多多商品（需写入确认）。", TaskIdArgs, "shop")
def pdd_update_goods(ctx: ToolContext, a: TaskIdArgs) -> dict:
    task = ensure_write_allowed(ctx, a.task_id)
    writer = ctx.services._pdd_writer
    if writer is None:
        return {"ok": False, "message": "没有可用的写店铺通道，请按物料包人工修改"}
    goods_id = task.spec["goods_id"]
    prod = ctx.session.scalar(select(Product).where(Product.goods_id == goods_id))
    assert prod is not None
    before = GoodsInfo(goods_id=goods_id, name=prod.name, title=prod.title, price_fen=prod.price_fen, main_image_hash=prod.main_image_hash)
    patch = GoodsPatch(**task.output.get("patch", {}))
    res = writer.update_goods(goods_id, patch, before)
    if res.ok:
        # 系统自记改动事件；第二天快照对比时按 48 小时窗口去重
        for field, kind, old in (("main_image", "main_image", prod.main_image_hash), ("title", "title", prod.title)):
            new = getattr(patch, field)
            if new:
                ctx.session.add(ChangeEvent(goods_id=goods_id, type=kind, before=old, after=new,
                                            occurred_on=queries.business_day(ctx.session), source="task", task_id=task.id))
        task.output = {**task.output, "write_ref": res.ref, "before": before.model_dump()}
    audit(ctx.session, f"agent:{ctx.agent}", "shop.update_goods", f"goods:{goods_id}", {"task": task.id, "ok": res.ok})
    return res.model_dump()


@tool("push_to_dropship_tool", "把上架物料包推送到铺货代发工具（需写入确认）。", TaskIdArgs, "shop")
def push_to_dropship_tool(ctx: ToolContext, a: TaskIdArgs) -> dict:
    task = ensure_write_allowed(ctx, a.task_id)
    if ctx.services.dropship is None:
        return {"ok": False, "message": "铺货代发工具未接入，请按物料包人工上传"}
    pkg = ListingPackage(**task.output["package"])
    res = ctx.services.dropship.push_listing(pkg)
    audit(ctx.session, f"agent:{ctx.agent}", "shop.push_listing", f"task:{task.id}", {"ok": res.ok})
    return res.model_dump()
