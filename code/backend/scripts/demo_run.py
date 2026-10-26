"""演示：全部模拟适配器下跑一遍「海盐换白底图」闭环和一次周选品，打印飞书里会看到的内容。

用法：python scripts/demo_run.py（使用临时数据库，不影响 data/app.db）
"""
from __future__ import annotations

import logging
import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("DSZNT_SECRETS", "memory")
os.environ["DSZNT_ADAPTER_MODE"] = "fake"

from app import bootstrap  # noqa: E402
from app.data import router  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.interfaces.feishu.handler import on_card_action  # noqa: E402
from app.llm.gateway import Gateway  # noqa: E402
from app.models import LlmUsage, Shop, Task, TestRun  # noqa: E402
from app.services import build_services, set_services  # noqa: E402
from app.workflow.jobs import enqueue, run_until_idle  # noqa: E402
from app.workflow.pipelines import DAILY_ITEMS  # noqa: E402

DAY = date(2026, 10, 23)
OWNER = "ou_demo_owner"


def show(card: dict) -> None:
    print(f"\n┌─ 飞书卡片：{card['header']['title']['content']}")
    for e in card["elements"]:
        if e["tag"] == "markdown":
            for line in e["content"].splitlines():
                print(f"│ {line}")
        elif e["tag"] == "action":
            print("│ " + "  ".join(f"[{a['text']['content']}]" for a in e["actions"]))
    print("└" + "─" * 40)


def buttons(card: dict) -> list[dict]:
    return [a["value"] for e in card["elements"] if e.get("tag") == "action" for a in e["actions"]]


def main() -> None:
    db = Path(tempfile.mkdtemp()) / "demo.db"
    bootstrap.init(f"sqlite:///{db.as_posix()}")
    logging.getLogger().setLevel(logging.WARNING)
    bootstrap.ensure_shop("示例香薰店")
    bootstrap.set_owner(OWNER, "店主")
    services = build_services()
    set_services(services)
    gateway = Gateway(services.llm)

    print("① 补采前 14 天数据（拼多多开放平台未申请 → 自动走网页读取通道）")
    with session_scope() as s:
        shop = s.query(Shop).first()
        for i in range(14, 0, -1):
            for item in DAILY_ITEMS:
                router.fetch(s, services, shop, item, DAY - timedelta(days=i))
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    show(services.feishu.sent[-1]["card"])

    print("\n② 店主在早报上点「同意」")
    approve = next(v for v in buttons(services.feishu.sent[-1]["card"]) if v["event"] == "approve")
    with session_scope() as s:
        print("   回调立即返回：", on_card_action(s, OWNER, approve)["toast"]["content"])
    run_until_idle(services, gateway)
    show(services.feishu.sent[-1]["card"])

    print("\n③ 店主点「确认写入」")
    confirm = next(v for v in buttons(services.feishu.sent[-1]["card"]) if v["event"] == "confirm")
    with session_scope() as s:
        on_card_action(s, OWNER, confirm)
    run_until_idle(services, gateway)
    with session_scope() as s:
        t = s.query(Task).one()
        print(f"   任务 #{t.id} 状态：{t.status}；写店铺调用：{services._pdd_writer.calls}")

    print("\n④ 每周选品")
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
    run_until_idle(services, gateway)
    show(services.feishu.sent[-1]["card"])

    print("\n⑤ 店主点「同意上新」→ 生成物料包 → 写入确认 → 推送铺货工具 → 开始测款")
    approve = next(v for v in buttons(services.feishu.sent[-1]["card"]) if v["event"] == "approve")
    with session_scope() as s:
        on_card_action(s, OWNER, approve)
    run_until_idle(services, gateway)
    show(services.feishu.sent[-1]["card"])
    confirm = next(v for v in buttons(services.feishu.sent[-1]["card"]) if v["event"] == "confirm")
    with session_scope() as s:
        on_card_action(s, OWNER, confirm)
    run_until_idle(services, gateway)
    with session_scope() as s:
        tr = s.query(TestRun).one()
        print(f"   已推送铺货工具 {len(services.dropship.pushed)} 次；测款从 {tr.start_date} 开始，满 14 天后发测款结论卡片")

    with session_scope() as s:
        rows = s.query(LlmUsage).all()
        print(f"\n模型调用 {len(rows)} 次（模拟），按真实单价折算 ¥{sum(r.cost_cny for r in rows):.3f}")


if __name__ == "__main__":
    main()
