from app.data.channels import manual_import
from app.db import session_scope
from app.models import DailyMetric, FetchJob, Shop

HEADER = "日期,商品ID,商品名称,商品访客数,商品加购件数,支付订单数,支付金额\n"


def _write(tmp_path, body: str, name="export.csv"):
    p = tmp_path / name
    p.write_text(HEADER + body, encoding="utf-8-sig")
    return p


def test_import_by_header_signature(env, tmp_path):
    path = _write(tmp_path, "2026-10-23,g_haiyan,海盐,350,20,8,239.20\n2026-10-23,g_citrus,柑橘,420,55,22,657.80\n",
                  name="随便起的名字.csv")
    with session_scope() as s:
        rec = manual_import.import_file(s, s.query(Shop).first(), path, "ou_operator")
        assert (rec.status, rec.rows, rec.parser) == ("imported", 2, "pdd_goods_traffic_v1")
        m = s.query(DailyMetric).filter_by(goods_id="g_haiyan").one()
        assert (m.visitors, m.orders, m.gmv_fen, m.source) == (350, 8, 23920, "manual")
        assert s.query(FetchJob).filter_by(item="goods_daily_traffic").one().status == "success"


def test_unrecognized_file(env, tmp_path):
    p = tmp_path / "x.csv"
    p.write_text("a,b\n1,2\n", encoding="utf-8")
    with session_scope() as s:
        assert manual_import.import_file(s, s.query(Shop).first(), p, "u").status == "unrecognized"


def test_bad_rows_reported(env, tmp_path):
    path = _write(tmp_path, "2026-10-23,g_haiyan,海盐,abc,20,8,1\n")
    with session_scope() as s:
        rec = manual_import.import_file(s, s.query(Shop).first(), path, "u")
        assert rec.status == "invalid" and rec.error_rows[0]["row"] == 2
        assert s.query(DailyMetric).count() == 0
