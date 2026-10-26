"""端到端：全部模拟适配器下跑通「采集 → 早报 → 拍板 → 派活 → 执行 → 写入确认 → 写店铺 → 验收 → 完成」。

用例即视频里的「海盐换白底图」：10-17 海盐主图换成白底图，之后转化率下降。
"""
from app.adapters.llm.fake import FakeLLM
from app.db import session_scope
from app.interfaces.feishu.handler import on_card_action
from app.llm.gateway import Gateway
from app.models import Candidate, ChangeEvent, LlmUsage, Report, Suggestion, Task
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER, backfill


def _buttons(card: dict) -> list[dict]:
    return [a["value"] for e in card["elements"] if e.get("tag") == "action" for a in e["actions"]]


def _run_daily(services, gateway):
    backfill(services, DAY, 14)
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)


def test_daily_loop_haiyan(env):
    services, gateway = env
    _run_daily(services, gateway)

    with session_scope() as s:
        ev = s.query(ChangeEvent).filter_by(goods_id="g_haiyan", type="main_image").one()
        assert ev.occurred_on.isoformat() == "2026-10-17" and ev.source == "snapshot"
        report = s.query(Report).filter_by(kind="daily", period=DAY.isoformat()).one()
        assert report.content["best"] == "柑橘香薰蜡烛"
        sug = s.query(Suggestion).filter_by(status="reported").one()
        assert sug.goods_id == "g_haiyan" and "换回" in sug.action and sug.confidence == "high"
        sug_id = sug.id
        assert s.query(LlmUsage).count() >= 2  # 运营诊断 + CEO 早报都有计量

    report_card = services.feishu.sent[0]["card"]
    approve = next(v for v in _buttons(report_card) if v["event"] == "approve" and v["id"] == sug_id)

    # 运营点「同意」→ 无权限；店主点 → 进入派活
    with session_scope() as s:
        assert "权限" in on_card_action(s, OPERATOR, approve)["toast"]["content"]
    with session_scope() as s:
        assert on_card_action(s, OWNER, approve)["toast"]["content"] == "已收到，处理中"
    with session_scope() as s:
        assert "已经处理过" in on_card_action(s, OWNER, approve)["toast"]["content"]  # 重复点击幂等
    run_until_idle(services, gateway)

    with session_scope() as s:
        task = s.query(Task).one()
        assert task.executor == "image" and task.status == "awaiting_write_confirm"
        assert task.output["patch"]["main_image"] == "img_g_haiyan_scene_v1"  # 换回改动前的场景图
    assert services._pdd_writer.calls == []  # 写入确认前没有写店铺

    confirm_card = services.feishu.sent[-1]["card"]
    confirm = next(v for v in _buttons(confirm_card) if v["event"] == "confirm")
    with session_scope() as s:
        on_card_action(s, OWNER, confirm)
    run_until_idle(services, gateway)

    with session_scope() as s:
        task = s.query(Task).one()
        assert task.status == "accepted" and task.write_confirmed_by == OWNER
        assert s.get(Suggestion, sug_id).status == "done"
        assert s.query(ChangeEvent).filter_by(source="task", goods_id="g_haiyan").count() == 1
    assert services._pdd_writer.calls == [("update_goods", {"goods_id": "g_haiyan", "main_image": "img_g_haiyan_scene_v1"})]
    # 早报卡片里这条建议的按钮已被替换成结果
    assert any("已同意" in str(u["card"]) for u in services.feishu.updated)


def test_fabricated_numbers_are_dropped(env):
    """运营 Agent 两次都编造数字 → 该条建议被丢弃，不进早报。"""
    services, _ = env

    def liar(req):
        return {"summary": "整体平稳", "suggestions": [{
            "goods_id": "g_haiyan", "type": "main_image", "action": "建议换回场景图",
            "reason": "因为转化率暴跌了 87.3%", "evidence": [], "confidence": "high", "needs_write": True}]}

    llm = FakeLLM({"ops.diagnose": liar})
    services.llm = llm
    gateway = Gateway(llm)
    _run_daily(services, gateway)
    with session_scope() as s:
        assert s.query(Suggestion).count() == 0
    # 第一次不通过后，模型收到了纠正提示
    retries = [r for r in llm.requests if r.agent == "ops.diagnose"]
    assert len(retries) == 2 and "87.3" in str(retries[1].messages[-1]["content"])


def test_weekly_selection(env):
    services, gateway = env
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
    run_until_idle(services, gateway)
    with session_scope() as s:
        rec = s.query(Candidate).filter_by(status="recommended").all()
        assert [c.offer_id for c in rec] == ["1688_6543210"]
        assert "建议上新" in rec[0].reason and rec[0].score >= 70
        from app.models import Demand

        grapefruit = s.query(Demand).filter_by(keyword="葡萄柚香薰蜡烛").one()
        rejected = {c.offer_id: c.reject_reason
                    for c in s.query(Candidate).filter_by(status="rejected", demand_id=grapefruit.id)}
        assert "不支持一件代发" in rejected["1688_7000001"]
        assert "得分" in rejected["1688_6543299"]  # 通过过滤和同款审查，但分数不够
        other = s.query(Demand).filter(Demand.id != grapefruit.id).all()
        assert all(c.status == "rejected" for d in other for c in s.query(Candidate).filter_by(demand_id=d.id))
    card = services.feishu.sent[-1]["card"]
    assert "选品周报" in card["header"]["title"]["content"]
    assert any(v["event"] == "approve" and v["entity"] == "candidate" for v in _buttons(card))


def test_llm_unavailable_degrades(env):
    services, _ = env
    services.llm = None
    _run_daily(services, Gateway(None))
    with session_scope() as s:
        from app.models import Job

        failed = s.query(Job).filter_by(type="daily.analyze").one()
        assert failed.status in ("queued", "failed") and "模型服务未配置" in failed.error
        assert s.query(Report).count() == 0
