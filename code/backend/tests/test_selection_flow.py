"""选品后半段：同意上新 → 物料包 → 写入确认 → 推送铺货工具 → 上架 → 14 天测款结论。"""
from datetime import timedelta

from app.agents import review
from app.db import session_scope
from app.interfaces.feishu.handler import on_card_action
from app.models import Candidate, DailyMetric, Task, TestRun
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OWNER, backfill


def _buttons(card):
    return [a["value"] for e in card["elements"] if e.get("tag") == "action" for a in e["actions"]]


def _click(services, event, entity):
    v = next(v for v in _buttons(services.feishu.sent[-1]["card"]) if v["event"] == event and v["entity"] == entity)
    with session_scope() as s:
        return on_card_action(s, OWNER, v)


def test_listing_flow_and_trial(env):
    services, gateway = env
    backfill(services, DAY, 1)  # 有业务日期即可
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
    run_until_idle(services, gateway)

    assert _click(services, "approve", "candidate")["toast"]["content"] == "已收到，处理中"
    run_until_idle(services, gateway)
    with session_scope() as s:
        task = s.query(Task).filter_by(ref_type="candidate").one()
        assert task.executor == "listing" and task.status == "awaiting_write_confirm"
        pkg = task.output["package"]
        assert pkg["price_fen"] == 2990 and pkg["main_images"] and pkg["source_offer_url"].startswith("https://detail.1688.com/")
    assert services.dropship.pushed == []  # 确认前不推送
    card = services.feishu.sent[-1]["card"]
    assert "写入确认" in card["header"]["title"]["content"] and "（新商品）" in str(card)

    _click(services, "confirm", "task")
    run_until_idle(services, gateway)
    assert len(services.dropship.pushed) == 1
    with session_scope() as s:
        cand = s.query(Candidate).filter_by(status="testing").one()
        tr = s.query(TestRun).one()
        assert tr.candidate_id == cand.id and tr.goods_id == "push_1" and tr.start_date == DAY - timedelta(days=1)
        assert review.run_due_trials(s, tr.start_date + timedelta(days=12)) == []  # 不满 14 天不判定

        # 模拟上架后 14 天：每天 30 访客、下单 2 → 访客 420、转化率 6.7%，高于店铺平均 → 留款
        for i in range(14):
            s.add(DailyMetric(goods_id="push_1", day=tr.start_date + timedelta(days=i), visitors=30, add_to_cart=5, orders=2))
        s.flush()
        backfill_end = tr.start_date + timedelta(days=13)
    backfill(services, backfill_end + timedelta(days=1), 14)  # 店铺其他商品同期数据，用于算平均转化率
    with session_scope() as s:
        results = review.run_due_trials(s, backfill_end)
        assert [(t.verdict, t.visitors) for t, _ in results] == [("keep", 420)]
        assert review.run_due_trials(s, backfill_end) == []  # 已判定的不再重复
