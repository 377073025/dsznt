"""选品扩展与 M5：延伸选品、以图搜货、合规审查、候选「改一改」、测款「优化」分支。"""
from datetime import timedelta

from app.adapters import fixtures
from app.agents import review, selection
from app.agents.runtime import AgentRunner
from app.db import session_scope
from app.interfaces.feishu import cards
from app.interfaces.feishu.handler import apply_action, on_card_action
from app.models import AdhocTask, Candidate, Demand, LlmUsage, Offer, Suggestion, TestRun, User
from app.workflow import adhoc
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill


def _weekly(services, gateway):
    backfill(services, DAY, 14)  # 有销量数据才知道哪款最好卖（延伸选品）
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
    run_until_idle(services, gateway)


def test_extension_demands_and_compliance_in_weekly(env):
    services, gateway = env
    _weekly(services, gateway)
    with session_scope() as s:
        demands = {d.keyword: d for d in s.query(Demand)}
        # 柑橘最好卖 → 同系列（香薰蜡烛）里不与本店重复的热词按「延伸」登记；柑橘香薰与本店商品重名不算
        assert demands["葡萄柚香薰蜡烛"].seed_type == "extension" and demands["葡萄柚香薰蜡烛"].seed_ref == "g_citrus"
        assert demands["香薰蜡烛礼盒"].seed_type == "extension"
        assert "柑橘香薰" not in demands or demands["柑橘香薰"].seed_type == "opportunity"
        rec = s.query(Candidate).filter_by(status="recommended").one()
        assert rec.compliance["passed"] is True and rec.breakdown["fit"] == 10  # 延伸选品契合度满分
        assert rec.backup_offer_ids == ["1688_6543299"]  # 通过审查但分数不够的同款作备选
        assert s.query(LlmUsage).filter_by(agent="selection.compliance").count() >= 1


def test_compliance_brand_words_skip_model(env):
    services, gateway = env
    with session_scope() as s:
        runner = AgentRunner(s, services, gateway)
        d = fixtures.offer_detail("1688_6543210").model_copy(update={"title": "祖玛珑同款 香薰蜡烛"})
        out = selection.check_compliance(runner, d)
        assert not out.passed and "祖玛珑" in out.issues[0]
        assert s.query(LlmUsage).filter_by(agent="selection.compliance").count() == 0  # 词库命中不调模型
        out = selection.check_compliance(runner, d.model_copy(update={"title": "仿大牌 香薰蜡烛"}))
        assert not out.passed and out.risk_level == "high"  # 词库没命中，交模型判断



def test_compliance_looks_at_web_images(env):
    services, gateway = env
    urls = [f"https://cbu01.alicdn.com/img/{i}.jpg" for i in range(5)]
    d = fixtures.offer_detail("1688_6543210").model_copy(update={"image_urls": ["local.jpg", *urls]})
    with session_scope() as s:
        runner = AgentRunner(s, services, gateway)
        assert selection.check_compliance(runner, d).passed
        content = services.llm.requests[-1].messages[0]["content"]
        assert [b["source"]["url"] for b in content if b["type"] == "image"] == urls[:3]  # 只发网络图片，最多 max_images 张
        assert "商标" in content[-1]["text"]

        calls = []

        def flaky(req):  # 图片取不到时模型服务报错 → 退回只看文字
            calls.append(req)
            if isinstance(req.messages[0]["content"], list):
                raise RuntimeError("image fetch failed")
            return {"passed": True, "risk_level": "low", "issues": []}

        services.llm.responders["selection.compliance"] = flaky
        assert selection.check_compliance(runner, d).passed
        assert [type(c.messages[0]["content"]) for c in calls] == [list, str]


def _revise_candidate(services, gateway, feedback):
    with session_scope() as s:
        cand = s.query(Candidate).filter_by(status="recommended").one()
        assert on_card_action(s, OWNER, {"entity": "candidate", "id": cand.id, "event": "revise",
                                         "version": cand.version})["toast"]["content"] == "已收到，处理中"
    run_until_idle(services, gateway)
    with session_scope() as s:
        enqueue(s, "feishu.message", {"open_id": OWNER, "chat_id": "oc_g", "chat_type": "group", "message_id": "om_r",
                                      "msg_type": "text", "text": feedback})
    run_until_idle(services, gateway)
    return cand.id


def test_candidate_revise_price_and_switch_offer(env):
    services, gateway = env
    _weekly(services, gateway)
    cid = _revise_candidate(services, gateway, "售价改成 32.9 元，换个货源")
    with session_scope() as s:
        c = s.get(Candidate, cid)
        assert c.status == "recommended" and c.revise_rounds == 1
        assert c.offer_id == "1688_6543299" and c.backup_offer_ids == ["1688_6543210"]
        assert c.price_plan["sell_price_fen"] == 3290 and "建议上新" in c.reason
    card = services.feishu.sent[-1]["card"]
    assert "修改后的候选（第 1 轮）" in card["header"]["title"]["content"]


def test_web_revise_endpoint(env):
    from fastapi.testclient import TestClient

    from app import main

    services, gateway = env
    _weekly(services, gateway)
    main.init = lambda **_: None
    with session_scope() as s:
        cid = s.query(Candidate).filter_by(status="recommended").one().id
    with TestClient(main.app) as c:
        r = c.post(f"/api/candidate/{cid}/revise", json={"feedback": "售价 35"}, headers={"X-Open-Id": OPERATOR})
        assert r.status_code == 403
        r = c.post(f"/api/candidate/{cid}/revise", json={"feedback": "售价 35"}, headers={"X-Open-Id": OWNER})
        assert r.json() == {"queued": True}
    run_until_idle(services, gateway)
    with session_scope() as s:
        c = s.get(Candidate, cid)
        assert c.status == "recommended" and c.price_plan["sell_price_fen"] == 3500


def test_optimize_branch_retests_once(env):
    services, gateway = env
    backfill(services, DAY, 14)
    with session_scope() as s:
        offer = selection._upsert_offer(s, fixtures.offer_detail("1688_6543210"))
        d = Demand(week="2026-W40", keyword="海盐香薰", seed_type="opportunity")
        s.add(d)
        s.flush()
        cand = Candidate(demand_id=d.id, offer_id=offer.id, status="testing")
        s.add(cand)
        s.flush()
        # 数据到 DAY 前一天，再观察从 DAY 开始。用海盐（转化率掉了、有主图改动）当作测款商品，诊断能给出建议
        s.add(TestRun(candidate_id=cand.id, goods_id="g_haiyan", start_date=DAY - timedelta(days=13), verdict="optimize"))
        s.flush()
        apply_action(s, services, OWNER, "candidate", cand.id, "optimize")
        cid = cand.id
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert s.get(Candidate, cid).status == "testing"
        tr = s.query(TestRun).filter_by(trial_round=2).one()
        assert tr.days == 7 and tr.start_date == DAY and tr.goods_id == "g_haiyan"
        sug = s.query(Suggestion).filter(Suggestion.reason.contains("测款优化")).one()
        assert sug.status == "reported" and sug.goods_id == "g_haiyan"
        # 再观察期满：第二轮不再给「优化」，按下架处理
        results = review.run_due_trials(s, tr.start_date + timedelta(days=6))
        assert [t.trial_round for t, _ in results] == [2] and results[0][0].verdict in ("keep", "drop")
    assert "测款优化" in services.feishu.sent[-1]["card"]["header"]["title"]["content"]
    card = cards.trial_result(Candidate(id=cid, version=1), "x", 10, 0.01, 0.03, "drop", 7, 2)
    events = [a["value"]["event"] for e in card["elements"] if e.get("tag") == "action" for a in e["actions"]]
    assert events == ["drop", "keep"] and "优化后再观察 7 天" in str(card)


def _run_adhoc(services, gateway, capability, params):
    with session_scope() as s:
        op = s.query(User).filter_by(feishu_open_id=OPERATOR).one()
        tid = adhoc.submit(s, op, capability, params).id
    run_until_idle(services, gateway)
    with session_scope() as s:
        return s.get(AdhocTask, tid)


def test_find_source_by_own_product_image(env):
    services, gateway = env
    backfill(services, DAY, 1)
    t = _run_adhoc(services, gateway, "selection.find_source", {"goods_id": "g_haiyan"})
    assert t.status == "done", t.error
    assert t.result["competitor_title"] == "海盐香薰蜡烛" and t.result["sell_price_fen"] == 2990  # 售价取本店商品
    assert len(t.result["offers"]) == 3  # 以图搜货
    t = _run_adhoc(services, gateway, "selection.find_source", {"keyword": "葡萄柚香薰蜡烛", "sell_price_fen": 2990})
    ok = [o for o in t.result["offers"] if o["passed"]]
    assert [o["offer_id"] for o in ok] == ["1688_6543210", "1688_6543299"] and ok[0]["compliance_issues"] == []


def test_image_modes_and_listing_to_candidate(env):
    services, gateway = env
    backfill(services, DAY, 1)
    t = _run_adhoc(services, gateway, "exec.image", {"goods_id": "g_haiyan", "mode": "remove_background"})
    assert t.status == "done" and t.result["path"].endswith("_nobg.jpg")
    t = _run_adhoc(services, gateway, "exec.listing", {"offer": "6543210", "keyword": "葡萄柚香薰", "sell_price_fen": 2990})
    assert t.status == "done" and t.result["reject_reasons"] == [] and t.result["score"] > 0
    with session_scope() as s:
        op = s.query(User).filter_by(feishu_open_id=OPERATOR).one()
        ref = adhoc.promote(s, services, op, t.id)
        cand = s.get(Candidate, int(ref.split(":")[1]))
        assert cand.status == "recommended" and "物料包" in cand.reason
        assert s.get(Offer, cand.offer_id).title.startswith("葡萄柚")
