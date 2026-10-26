import pytest

from app.adapters.llm.fake import FakeLLM
from app.agents.runtime import AgentRunner
from app.data.channels.web_reader import repair as R
from app.data.channels.web_reader.fake_page import FakeElement, FakePage
from app.data.channels.web_reader.script import run_script
from app.data.errors import CaptchaRequired, ForbiddenAction, RiskBlocked
from app.db import session_scope
from app.llm.gateway import Gateway
from app.models import WebScript

PLATFORM = "pdd_backend"
ROWS = [{"商品ID": "g_haiyan", "访客数": "350", "加购件数": "20", "支付订单数": "8"}]
STEPS = [
    {"goto": "https://mms.pinduoduo.com/goods/data"},
    {"wait_for": {"role": "table", "name_contains": "商品明细"}},
    {"read_table": {"target": {"role": "table", "name_contains": "商品明细"}}},
    {"map_columns": {"商品ID": "goods_id", "访客数": "visitors"}},
]


def _page(table_name="商品明细") -> FakePage:
    return FakePage(elements=[FakeElement("table", table_name), FakeElement("button", "发布商品")],
                    tables={table_name: ROWS})


def test_script_reads_table():
    res = run_script(_page(), STEPS, PLATFORM, {})
    assert res.rows == [{"goods_id": "g_haiyan", "visitors": "350"}]


def test_forbidden_click_rejected():
    steps = [{"click": {"role": "button", "name_contains": "发布"}}]
    with pytest.raises(ForbiddenAction):
        run_script(_page(), steps, PLATFORM, {})


def test_goto_other_domain_rejected():
    with pytest.raises(ForbiddenAction):
        run_script(_page(), [{"goto": "https://evil.example.com/"}], PLATFORM, {})


def test_unknown_action_rejected():
    with pytest.raises(ForbiddenAction):
        run_script(_page(), [{"execute_js": "alert(1)"}], PLATFORM, {})


def test_risk_detection():
    page = _page()
    page.text = "请拖动滑块完成验证"
    with pytest.raises(CaptchaRequired):
        run_script(page, STEPS[:1], PLATFORM, {})
    page.text = "访问受限"
    with pytest.raises(RiskBlocked):
        run_script(page, STEPS[:1], PLATFORM, {})


def _repair_responder():
    """模拟模型：先读改版后的表格，再报告完成。"""
    def respond(req):
        if not req.context["history"]:
            return {"done": False, "action": "read_table", "role": "table", "name_contains": "商品数据明细",
                    "value": "", "reason": "表格改名了"}
        return {"done": True, "action": "", "role": "", "name_contains": "", "value": "", "reason": "已读到"}
    return respond


def test_ai_repair_creates_candidate_script(env):
    services, _ = env
    page = _page("商品数据明细")  # 页面改版：表格改名
    llm = FakeLLM({"web.repair": _repair_responder()})
    with session_scope() as s:
        s.add(WebScript(task_name="pdd_backend.goods_traffic_daily", script_version=1, steps=STEPS, state="active"))
        s.flush()
        runner = AgentRunner(s, services, Gateway(llm))
        res = R.run_task(s, runner, page, "pdd_backend.goods_traffic_daily", PLATFORM, "读取商品明细", {})
        assert res.rows[0]["goods_id"] == "g_haiyan"
        new = R.active_script(s, "pdd_backend.goods_traffic_daily")
        assert new.script_version == 2 and new.updated_by == "ai" and new.state == "candidate"
        # 新脚本连续成功 2 次后转正，旧脚本退役
        for _ in range(2):
            R.run_task(s, runner, page, "pdd_backend.goods_traffic_daily", PLATFORM, "读取商品明细", {})
        s.flush()
        states = {w.script_version: w.state for w in s.query(WebScript).all()}
        assert states == {1: "retired", 2: "active"}


def test_ai_repair_cannot_click_write_button(env):
    services, _ = env
    llm = FakeLLM({"web.repair": lambda req: {"done": False, "action": "click", "role": "button",
                                              "name_contains": "发布商品", "value": "", "reason": "试试"}})
    with session_scope() as s:
        s.add(WebScript(task_name="t", script_version=1, steps=STEPS, state="active"))
        s.flush()
        runner = AgentRunner(s, services, Gateway(llm))
        with pytest.raises(ForbiddenAction):
            R.run_task(s, runner, _page("改版表格"), "t", PLATFORM, "读取", {})
