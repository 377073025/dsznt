"""执行 Agent：文案、图片、上架物料。启动时校验任务状态；写店铺的步骤必须经过写入确认。"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.adapters.types import ListingPackage
from app.agents.runtime import AgentRunner, AgentSpec
from app.config import get_settings
from app.models import Candidate, ChangeEvent, Demand, Offer, Product, Task
from app.services import Services
from app.tools.registry import ToolContext, tools_for
from app.workflow.state_machine import Actor, transition

EXECUTOR = Actor("agent", "executor")


class CopyOut(BaseModel):
    title: str
    selling_points: list[str]


COPY = AgentSpec(name="exec.copy", model_role="main", output=CopyOut, tool_names=[])


def execute(runner: AgentRunner, s: Session, services: Services, task_id: int) -> Task:
    task = s.get(Task, task_id)
    assert task is not None
    transition(s, "task", task.id, "start", EXECUTOR)
    try:
        output = _produce(runner, s, services, task)
    except Exception as e:
        transition(s, "task", task.id, "fail", EXECUTOR, {"error": str(e)[:300]})
        raise
    task.output = output
    transition(s, "task", task.id, "produce", EXECUTOR)
    transition(s, "task", task.id, "request_write" if task.needs_write else "submit", EXECUTOR)
    return task


def _produce(runner: AgentRunner, s: Session, services: Services, task: Task) -> dict:
    goods_id = task.spec.get("goods_id", "")
    prod = s.scalar(select(Product).where(Product.goods_id == goods_id))
    if task.executor == "image":
        # 有主图改动记录时优先换回改动前的图（素材库保留历史版本）；否则交生图服务换背景
        ev = s.scalar(
            select(ChangeEvent).where(ChangeEvent.goods_id == goods_id, ChangeEvent.type == "main_image")
            .order_by(ChangeEvent.occurred_on.desc())
        )
        if ev is not None and "换回" in task.spec.get("instruction", ""):
            return {"patch": {"main_image": ev.before}, "preview": ev.before, "method": "revert"}
        if services.image_gen is None:
            raise RuntimeError("生图服务未接入（能力 image_gen）")
        new = services.image_gen.replace_background(Path(f"{prod.main_image_hash if prod else goods_id}.jpg"), task.spec["instruction"])
        return {"patch": {"main_image": new.stem}, "preview": str(new), "method": "image_gen"}
    if task.executor == "copy":
        out = runner.run(COPY, f"为商品 {prod.name if prod else goods_id} 改写标题：{task.spec['instruction']}",
                         context={"goods_id": goods_id, "name": prod.name if prod else ""})
        return {"patch": {"title": out.title}, "selling_points": out.selling_points}
    if task.executor == "listing":
        return _listing_package(runner, s, task)
    raise NotImplementedError(f"执行器 {task.executor} 将在 M4 实现")


def _listing_package(runner: AgentRunner, s: Session, task: Task) -> dict:
    """上架物料包：标题卖点（模型）+ 货源图 + 售价 + SKU + 货源链接，存到 packages 目录。

    修图、换背景暂未接入：先用货源原图，后续由图片执行器生成新版本替换。
    """
    cand = s.get(Candidate, task.ref_id)
    offer = s.get(Offer, cand.offer_id)
    demand = s.get(Demand, cand.demand_id)
    copy = runner.run(COPY, f"为 1688 货源「{offer.title}」写拼多多上新标题与卖点，核心需求词：{demand.keyword}",
                      context={"goods_id": offer.id, "name": demand.keyword})
    sell = cand.price_plan["sell_price_fen"]
    pkg = ListingPackage(
        title=copy.title,
        main_images=offer.image_urls[:5],
        skus=[{"name": "默认", "price_fen": sell, "source_offer_id": offer.id}],
        price_fen=sell,
        source_offer_url=f"https://detail.1688.com/offer/{offer.id.removeprefix('1688_')}.html",
    )
    out_dir = get_settings().path("packages_dir")
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"candidate_{cand.id}.json"
    path.write_text(pkg.model_dump_json(indent=2), encoding="utf-8")
    pkg.path = str(path)
    return {"package": pkg.model_dump(), "selling_points": copy.selling_points}


def write(s: Session, services: Services, task_id: int) -> Task:
    """写入确认后执行：通过 shop 级工具写店铺（工具内部再校验一次）。

    上新走铺货代发工具；修改已有商品走拼多多写接口。
    """
    task = s.get(Task, task_id)
    assert task is not None and task.status == "writing"
    tool_name = "push_to_dropship_tool" if task.executor == "listing" else "pdd_update_goods"
    tool = tools_for("exec.writer", [tool_name])[0]
    ctx = ToolContext(s, services, "exec.writer", task.id)
    res = json.loads(tool.invoke(ctx, {"task_id": task.id}))
    if res.get("ok"):
        task.output = {**task.output, "write_ref": res.get("ref", "")}
        transition(s, "task", task.id, "submit", EXECUTOR, {"write_ref": res.get("ref", "")})
    else:
        transition(s, "task", task.id, "fail", EXECUTOR, {"error": res.get("message", "")})
    return task
