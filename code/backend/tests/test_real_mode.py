"""真实模式冒烟：只接了模型和飞书（这里用模拟实现代替），店铺数据全靠人工导出文件。

验证现在就能用的部分在真实部署下真能跑：采集降级到人工 → 卡片带导出指引 → 上传三份文件 → 早报和建议；
其余没接通的能力（周选品、货源复查、修图）不报错、只是跳过或给出明确原因。
"""
import json
from datetime import timedelta

import pytest

from app import bootstrap
from app import capabilities as caps
from app.adapters import fixtures
from app.adapters.feishu.fake import FakeFeishu
from app.adapters.llm.fake import FakeLLM
from app.data.channels import manual_import
from app.db import session_scope
from app.llm.gateway import Gateway
from app.models import AdhocTask, FetchJob, Report, Shop, Suggestion, User
from app.services import Services, set_services
from app.workflow import adhoc, on_sale
from app.workflow.jobs import enqueue, run_until_idle
from tests.conftest import DAY, OPERATOR, OWNER


@pytest.fixture
def real_env(tmp_path, monkeypatch):
    monkeypatch.setenv("DSZNT_ADAPTER_MODE", "real")
    bootstrap.init(f"sqlite:///{(tmp_path / 'real.db').as_posix()}")
    bootstrap.ensure_shop("示例香薰店", "shop_demo")
    bootstrap.set_owner(OWNER, "店主")
    with session_scope() as s:
        s.add(User(feishu_open_id=OPERATOR, name="运营", role="operator"))
        assert {c: caps.is_healthy(s, c) for c in ("llm", "pdd_web", "alibaba_web")} == \
            {"llm": False, "pdd_web": False, "alibaba_web": False}
        # 录入密钥后健康检查通过的样子
        for key in ("llm", "feishu_bot"):
            caps.set_status(s, key, caps.HEALTHY)
    services = Services(mode="real", llm=FakeLLM(), feishu=FakeFeishu())
    set_services(services)
    yield services, Gateway(services.llm)
    from app.workflow import rule_config

    rule_config._replace(rule_config._baseline())
    rule_config._applied_id = None


def _csv(path, header, rows):
    path.write_text(",".join(header) + "\n" + "\n".join(",".join(str(c) for c in r) for r in rows) + "\n", encoding="utf-8-sig")
    return path


def _exports(tmp_path):
    """按人工导入解析器的表头，从模拟数据生成三份「拼多多后台导出」。"""
    days = [DAY - timedelta(days=i) for i in range(14, -1, -1)]
    goods = _csv(tmp_path / "商品列表.csv", ["商品ID", "商品名称", "拼单价", "商品主图"],
                 [(g.goods_id, g.title, f"{g.price_fen / 100:.2f}", f"https://img.example.com/{g.main_image_hash}.jpg")
                  for g in fixtures.goods_list(DAY)])
    traffic = _csv(tmp_path / "商品数据.csv", ["日期", "商品ID", "商品访客数", "商品加购件数", "支付订单数", "支付金额"],
                   [(d.isoformat(), t.goods_id, t.visitors, t.add_to_cart, t.orders, f"{t.gmv_fen / 100:.2f}")
                    for d in days for t in fixtures.goods_traffic(d)])
    orders = _csv(tmp_path / "交易数据.csv", ["日期", "成交订单数", "成交金额", "退款订单数"],
                  [(d.isoformat(), o.orders, f"{o.gmv_fen / 100:.2f}", o.refunds) for d in days for o in [fixtures.order_stats(d)]])
    return [goods, traffic, orders]


def test_daily_loop_runs_on_manual_imports(real_env, tmp_path):
    services, gateway = real_env
    with session_scope() as s:
        enqueue(s, "daily.fetch", {"day": DAY.isoformat()})
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert {j.item: j.status for j in s.query(FetchJob)} == {
            "goods_snapshot": "manual_requested", "goods_daily_traffic": "manual_requested", "order_stats": "manual_requested"}
    asks = [json.dumps(m["card"], ensure_ascii=False) for m in services.feishu.sent if "card" in m]
    assert len(asks) == 3 and any("商品ID、商品名称、拼单价" in a for a in asks)  # 卡片里直接带导出步骤

    for f in _exports(tmp_path):
        with session_scope() as s:
            rec = manual_import.import_file(s, s.query(Shop).one(), f, OPERATOR)
            assert rec.status == "imported", (f.name, rec.error_rows)
        run_until_idle(services, gateway)
    with session_scope() as s:
        report = s.query(Report).filter_by(kind="daily", period=DAY.isoformat()).one()
        assert report.content and s.query(Suggestion).filter_by(day=DAY).count() >= 1


def test_unconnected_capabilities_skip_cleanly(real_env, tmp_path, monkeypatch):
    services, gateway = real_env
    for f in _exports(tmp_path):
        with session_scope() as s:
            manual_import.import_file(s, s.query(Shop).one(), f, OPERATOR)
    with session_scope() as s:
        enqueue(s, "selection.weekly", {"week": "2026-W43", "category": "香薰蜡烛"})
        assert on_sale.watch_offers(s, services, DAY) == []
    run_until_idle(services, gateway)  # 周选品没有市场数据和货源通道：不报错，没有候选

    from datetime import datetime

    monkeypatch.setattr(adhoc, "_now_cn", lambda: datetime(2026, 10, 24, 10, 0))
    with session_scope() as s:
        op = s.query(User).filter_by(feishu_open_id=OPERATOR).one()
        ask = adhoc.submit(s, op, "ceo.ask", {"question": "昨天哪款卖得最好？"}).id
        copy = adhoc.submit(s, op, "exec.copy", {"goods_id": "g_haiyan", "instruction": "突出助眠"}).id
        image = adhoc.submit(s, op, "exec.image", {"goods_id": "g_haiyan", "mode": "replace_background",
                                                   "instruction": "白底"}).id
        offer = adhoc.submit(s, op, "selection.evaluate_offer", {"offer": "1688_6543210", "sell_price_fen": 2990}).id
    run_until_idle(services, gateway)
    with session_scope() as s:
        assert [s.get(AdhocTask, i).status for i in (ask, copy)] == ["done", "done"]
        # 没接通的能力：任务失败并写明缺什么（不是程序异常）
        assert s.get(AdhocTask, image).status == "failed" and "image_gen" in s.get(AdhocTask, image).error
        assert s.get(AdhocTask, offer).status == "failed" and "alibaba_api/alibaba_web" in s.get(AdhocTask, offer).error
