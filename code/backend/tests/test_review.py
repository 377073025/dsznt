from datetime import date, timedelta

from app.agents import review
from app.db import session_scope
from app.models import Review
from app.workflow.jobs import dispatch_events
from tests.conftest import backfill

END = date(2026, 10, 24)  # 海盐 10-17 换白底图，满 7 天后复盘


def test_due_review_detects_worse_conversion(env):
    services, _ = env
    backfill(services, END + timedelta(days=1), 15)
    dispatch_events()  # 快照到齐的事件触发改动检测
    with session_scope() as s:
        out = review.run_due_reviews(s, END)
        haiyan = [r for r in out if r.after["cvr_pct"] < r.before["cvr_pct"]]
        assert len(out) == 1 and haiyan
        rv = out[0]
        assert rv.verdict == "变差" and rv.before["cvr_pct"] > 3.5 and rv.after["cvr_pct"] < 2.5
        text = review.describe(s, rv)
        assert text.startswith("海盐香薰蜡烛 2026-10-17 改主图") and "变差" in text
        assert review.run_due_reviews(s, END) == []  # 同一改动只复盘一次
        assert s.query(Review).count() == 1


def test_review_not_due_before_window(env):
    services, _ = env
    backfill(services, END, 14)
    with session_scope() as s:
        assert review.run_due_reviews(s, date(2026, 10, 23)) == []
