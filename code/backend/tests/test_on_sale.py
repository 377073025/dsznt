"""在售款：上新商品 ID 回填、货源复查 offer_watch、月度调权提议。"""
from datetime import timedelta

from app.adapters import fixtures
from app.config import get_settings
from app.db import session_scope
from app.interfaces.feishu.handler import on_card_action
from app.models import Candidate, Offer, Product, RuleConfig, RuleProposal, TestRun
from app.rules.selection import tune_weights
from app.workflow import on_sale, tuning
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill
from tests.test_selection_flow import _click


def _listed(services, gateway):
    """走完周选品 → 同意 → 写入确认 → 推送铺货工具 → 测款开始。"""
    backfill(services, DAY, 1)
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
    run_until_idle(services, gateway)
    _click(services, "approve", "candidate")
    run_until_idle(services, gateway)
    _click(services, "confirm", "task")
    run_until_idle(services, gateway)


def test_backfill_goods_id_from_snapshot(env):
    services, gateway = env
    _listed(services, gateway)
    with session_scope() as s:
        assert s.query(TestRun).one().goods_id == "push_1"
    # 第二天的商品快照里出现了标题相同的新商品 → 回填拼多多商品 ID，并记下货源
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    with session_scope() as s:
        tr = s.query(TestRun).one()
        cand = s.get(Candidate, tr.candidate_id)
        assert tr.goods_id == "g_new_1"
        assert s.query(Product).filter_by(goods_id="g_new_1").one().source_offer_id == cand.offer_id
        assert on_sale.backfill_listed_ids(s, DAY) == []  # 已回填的不再处理


def test_backfill_needs_unique_match(env):
    services, gateway = env
    _listed(services, gateway)
    services.dropship.pushed.append(services.dropship.pushed[0])  # 两个同标题新品：无法确定是哪个，不回填
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert s.query(TestRun).one().goods_id == "push_1"


def _watch(services, day=DAY):
    with session_scope() as s:
        return on_sale.watch_offers(s, services, day)


def test_offer_watch_alerts_with_backups(env, monkeypatch):
    services, gateway = env
    monkeypatch.setattr(fixtures, "_OFFERS", dict(fixtures._OFFERS))  # 改副本，不影响其他测试的货源顺序
    _listed(services, gateway)
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    with session_scope() as s:
        oid = s.query(Product).filter_by(goods_id="g_new_1").one().source_offer_id
        backups = s.query(Candidate).filter_by(offer_id=oid, status="testing").one().backup_offer_ids
    assert _watch(services) == []  # 货源正常不告警

    # 涨价 3% 不告警；累计涨到 8% 告警一次，当天不重复
    d = fixtures._OFFERS[oid]
    fixtures._OFFERS[oid] = d.model_copy(update={"price_fen": round(d.price_fen * 1.03)})
    assert _watch(services) == []
    fixtures._OFFERS[oid] = d.model_copy(update={"price_fen": round(d.price_fen * 1.08), "stock": 50})
    alerts = _watch(services, DAY + timedelta(days=1))
    assert len(alerts) == 1 and alerts[0]["goods_id"] == "g_new_1"
    assert any("涨 8%" in x for x in alerts[0]["issues"]) and any("库存 50" in x for x in alerts[0]["issues"])
    assert [b["offer_id"] for b in alerts[0]["backups"]][: len(backups)] == backups and alerts[0]["backups"][0]["checked"]
    assert oid not in [b["offer_id"] for b in alerts[0]["backups"]]
    card = services.feishu.sent[-1]["card"]
    assert "货源异常" in card["header"]["title"]["content"] and "备选货源" in str(card)
    assert _watch(services, DAY + timedelta(days=1)) == []
    with session_scope() as s:
        assert s.get(Offer, oid).raw["watch_base_fen"] == round(d.price_fen * 1.08)  # 告警后基准更新

    # 货源下架
    del fixtures._OFFERS[oid]
    alerts = _watch(services, DAY + timedelta(days=2))
    assert alerts[0]["issues"] == ["货源已下架或无法访问"]


def test_offer_watch_image_search_backups(env):
    """没有选品时的备选：以图搜同款，按现售价过一遍硬性过滤（不支持代发的排除）。"""
    services, _ = env
    backfill(services, DAY, 1)
    with session_scope() as s:
        s.query(Product).filter_by(goods_id="g_citrus").one().source_offer_id = "1688_gone"
    alerts = _watch(services)
    assert alerts[0]["issues"] == ["货源已下架或无法访问"]
    assert [b["offer_id"] for b in alerts[0]["backups"]] == ["1688_6543210", "1688_6543299"]
    assert not alerts[0]["backups"][0]["checked"]


def test_tune_weights_pure():
    w = {"profit": 25, "demand": 20, "competition": 10, "supplier": 15, "dropship_auto": 15, "fit": 10, "assets": 5}
    cfg = {"min_samples": 6, "max_step": 3}
    win = {"profit": 22, "demand": 18, "competition": 5, "supplier": 12, "dropship_auto": 15, "fit": 10, "assets": 3}
    lose = {"profit": 10, "demand": 17, "competition": 6, "supplier": 12, "dropship_auto": 15, "fit": 9, "assets": 3}
    assert tune_weights(w, [(win, True)] * 3 + [(lose, False)] * 2, cfg)[0] == w  # 样本不足
    new, stats = tune_weights(w, [(win, True)] * 3 + [(lose, False)] * 3, cfg)
    assert sum(new.values()) == 100 and new["profit"] == 28  # 留款利润得分率明显更高 → +3（封顶）
    assert stats["profit"] == {"win": 0.88, "lose": 0.4, "delta": 3} and all(abs(new[k] - w[k]) <= 3 for k in w)


def _seed_trials(s):
    cand = s.query(Candidate).first()
    win = {"profit": 22, "demand": 18, "competition": 5, "supplier": 12, "dropship_auto": 15, "fit": 10, "assets": 3}
    lose = {**win, "profit": 10}
    for i in range(6):
        s.add(Candidate(demand_id=cand.demand_id, offer_id=cand.offer_id, breakdown=win if i < 3 else lose,
                        status="kept" if i < 3 else "delisted"))


def test_monthly_tuning_proposal_and_apply(env):
    services, gateway = env
    backfill(services, DAY, 14)
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
    run_until_idle(services, gateway)
    with session_scope() as s:
        _seed_trials(s)
        enqueue(s, "rules.monthly_tuning", {"period": "2026-11"})
    run_until_idle(services, gateway)
    note = services.feishu.sent[-1]
    assert note["open_id"] == OWNER and "月度调权提议 · 2026-11" in note["card"]["header"]["title"]["content"]
    assert "利润" in str(note["card"]) and "25 → 28" in str(note["card"])
    btn = next(a["value"] for e in note["card"]["elements"] if e.get("tag") == "action" for a in e["actions"]
               if a["value"]["event"] == "apply")
    with session_scope() as s:
        assert "权限" in on_card_action(s, OPERATOR, btn)["toast"]["content"]  # 只有店主能同意
        assert on_card_action(s, OWNER, btn)["toast"]["content"] == "已收到，处理中"
    run_until_idle(services, gateway)
    with session_scope() as s:
        p = s.query(RuleProposal).one()
        assert p.status == "applied" and p.rule_version == 1
        assert s.query(RuleConfig).one().approved_by == f"user:{OWNER}"
    assert get_settings().rules["selection"]["score_weights"]["profit"] == 28

    # 下个月再提议时，上个月没处理的作废
    with session_scope() as s:
        p2 = tuning.propose_weights(s, "2026-12")
        p3 = tuning.propose_weights(s, "2027-01")
        assert p2 and p3 and s.get(RuleProposal, p2.id).status == "expired"
