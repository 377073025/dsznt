from datetime import date

from app.adapters import fixtures
from app.config import get_settings
from app.rules import metrics as M
from app.rules import selection as R

RULES = get_settings().rules["selection"]


def test_profit_formula():
    offer = fixtures.offer_detail("1688_6543210")
    p = R.profit(2990, offer, RULES["profit"])
    # 平台费 0.6% + 推广 10% + 售后 3% = 13.6% × 2990 ≈ 407，加工具费 20 分
    assert p.fees_fen == 427
    assert p.profit_fen == 2990 - 1180 - 350 - 427
    assert round(p.margin, 3) == 0.345


def test_hard_filter_rejects_non_dropship_and_young_supplier():
    bad = fixtures.offer_detail("1688_7000001")
    reasons = R.hard_filter(bad, 2990, RULES["hard_filters"], RULES["profit"])
    assert "不支持一件代发" in reasons
    assert any("供应商经营" in r for r in reasons)
    good = fixtures.offer_detail("1688_6543210")
    assert R.hard_filter(good, 2990, RULES["hard_filters"], RULES["profit"]) == []


def test_hard_filter_rejects_low_margin():
    offer = fixtures.offer_detail("1688_6543210")
    reasons = R.hard_filter(offer, 1990, RULES["hard_filters"], RULES["profit"])
    assert any("毛利率" in r for r in reasons)


def test_score_weights_and_threshold():
    offer = fixtures.offer_detail("1688_6543210")
    p = R.profit(2990, offer, RULES["profit"])
    total, parts = R.score(offer, p, 8200, 0.34, 0.42, True, 0.9, 0.6, RULES["score_weights"])
    assert set(parts) == set(RULES["score_weights"])
    assert total >= RULES["candidate_min_score"]
    total_unknown, parts_unknown = R.score(offer, p, 8200, 0.34, 0.42, None, 0.9, 0.6, RULES["score_weights"])
    assert parts_unknown["dropship_auto"] == 0  # 代发自动化未知按 0 分
    assert total_unknown < total


def test_trial_verdict():
    cfg = RULES["test_run"]
    assert R.trial_verdict(400, 0.05, 0.05, cfg) == "keep"
    assert R.trial_verdict(80, 0.05, 0.05, cfg) == "drop"
    assert R.trial_verdict(400, 0.02, 0.05, cfg) == "drop"
    assert R.trial_verdict(200, 0.045, 0.05, cfg) == "optimize"


def test_cvr_anomaly_detects_haiyan_only():
    end = date(2026, 10, 23)
    rows = [
        M.DayRow(r.goods_id, r.day, r.visitors, r.add_to_cart, r.orders, r.gmv_fen)
        for d in range(14)
        for r in fixtures.goods_traffic(date.fromordinal(end.toordinal() - d))
    ]
    flagged = [g for g in ("g_citrus", "g_haiyan", "g_lavender", "g_cedar")
               if M.cvr_anomaly(M.compare(rows, g, end), 200, 0.7)]
    assert flagged == ["g_haiyan"]


def test_cvr_anomaly_needs_enough_visitors():
    c = M.GoodsCompare("x", M.Window(150, 10, 1, 0), M.Window(150, 10, 10, 0))
    assert not M.cvr_anomaly(c, 200, 0.7)
