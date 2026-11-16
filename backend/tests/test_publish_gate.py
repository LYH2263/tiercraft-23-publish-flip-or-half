"""发布闸口：草稿/已发布双态隔离 + 对账一致才发布的端到端（服务层）测试。"""

import sqlite3

import pytest

import app.db as db
from app import seed
from app.services import pipeline

DOMAIN_ID = 1  # init_db 播种的「门店日销售」


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "app.db")
    seed.init_db()
    return tmp_path / "app.db"


def q(sql, args=()):
    conn = db.connect()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def one(sql, args=()):
    return q(sql, args)[0]


def build_to_draft():
    pipeline.run_paste_to_detail(DOMAIN_ID)
    pipeline.run_detail_to_summary(DOMAIN_ID)


def test_detail_to_summary_writes_only_draft(fresh_db):
    build_to_draft()
    counts = pipeline.layer_counts(DOMAIN_ID)
    assert counts["汇总层"] == 4  # 草稿：4 个 业务日+门店 桶
    assert counts["汇总层已发布"] == 0  # 已发布区保持为空
    assert one("SELECT COUNT(*) AS c FROM layer_summary_published")["c"] == 0


def test_publish_rejected_without_reconcile(fresh_db):
    build_to_draft()
    rec = pipeline.publish_summary(DOMAIN_ID)
    assert rec["status"] == "拒绝"
    assert rec["reconcile_report_id"] is None
    assert "对账" in rec["reason"]
    # 拒绝不留痕发布区
    assert one("SELECT COUNT(*) AS c FROM layer_summary_published")["c"] == 0
    # 但拒绝本身留痕、写明原因
    saved = one("SELECT * FROM publish_records WHERE id=?", (rec["id"],))
    assert saved["status"] == "拒绝"
    assert saved["reason"] == rec["reason"]


def test_publish_rejected_when_reconcile_differs(fresh_db):
    build_to_draft()
    # 直接往明细层塞一行，使明细与汇总脱节，对账必为「存在差异」
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO layer_detail(
              domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
            ) VALUES (?, '2026-11-12', 'S09', '城南店', '饮料', 10.0, 1, '已清洗')
            """,
            (DOMAIN_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    report = pipeline.run_reconcile(DOMAIN_ID)
    assert report["status"] == "存在差异"
    rec = pipeline.publish_summary(DOMAIN_ID)
    assert rec["status"] == "拒绝"
    assert rec["reconcile_report_id"] == report["id"]
    assert str(report["id"]) in rec["reason"]
    assert "存在差异" in rec["reason"]
    assert one("SELECT COUNT(*) AS c FROM layer_summary_published")["c"] == 0


def test_publish_success_snapshots_and_isolates(fresh_db):
    build_to_draft()
    report = pipeline.run_reconcile(DOMAIN_ID)
    assert report["status"] == "一致"

    rec = pipeline.publish_summary(DOMAIN_ID)
    assert rec["status"] == "成功"
    assert rec["reconcile_report_id"] == report["id"]
    assert rec["row_count"] == 4
    assert rec["created_at"]

    published = q(
        "SELECT * FROM layer_summary_published WHERE domain_id=? ORDER BY biz_day, store_code",
        (DOMAIN_ID,),
    )
    assert len(published) == 4
    assert {r["publish_id"] for r in published} == {rec["id"]}
    # 发布区内容与发布记录同源：逐列等于当时的草稿快照
    draft = q(
        "SELECT * FROM layer_summary WHERE domain_id=? ORDER BY biz_day, store_code",
        (DOMAIN_ID,),
    )
    for p, d in zip(published, draft):
        assert (p["biz_day"], p["store_code"]) == (d["biz_day"], d["store_code"])
        assert p["amount_sum"] == pytest.approx(d["amount_sum"])
        assert p["qty_sum"] == d["qty_sum"]
        assert p["line_count"] == d["line_count"]

    # 发布后再次明细到汇总：草稿被重写，已发布区一行不动
    first_ids = [r["id"] for r in published]
    build_to_draft()
    still = q(
        "SELECT * FROM layer_summary_published WHERE domain_id=? ORDER BY id", (DOMAIN_ID,)
    )
    assert [r["id"] for r in still] == first_ids
    assert {r["publish_id"] for r in still} == {rec["id"]}


def test_published_area_replaced_only_by_next_successful_publish(fresh_db):
    build_to_draft()
    pipeline.run_reconcile(DOMAIN_ID)
    first = pipeline.publish_summary(DOMAIN_ID)
    assert first["status"] == "成功"

    # 明细加一行并重跑汇总：草稿变 5 桶，已发布区仍是旧的 4 行
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO layer_detail(
              domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
            ) VALUES (?, '2026-11-12', 'S09', '城南店', '饮料', 10.0, 1, '已清洗')
            """,
            (DOMAIN_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    pipeline.run_detail_to_summary(DOMAIN_ID)
    assert pipeline.layer_counts(DOMAIN_ID)["汇总层"] == 5
    assert pipeline.layer_counts(DOMAIN_ID)["汇总层已发布"] == 4

    # 对账一致后再次发布：已发布区被整体替换，旧发布批次的行不残留
    pipeline.run_reconcile(DOMAIN_ID)
    second = pipeline.publish_summary(DOMAIN_ID)
    assert second["status"] == "成功"
    assert second["row_count"] == 5
    rows = q("SELECT * FROM layer_summary_published WHERE domain_id=?", (DOMAIN_ID,))
    assert len(rows) == 5
    assert {r["publish_id"] for r in rows} == {second["id"]}
    assert one(
        "SELECT COUNT(*) AS c FROM layer_summary_published WHERE publish_id=?",
        (first["id"],),
    )["c"] == 0


def test_dashboard_distinguishes_draft_and_published(fresh_db):
    build_to_draft()
    pipeline.run_reconcile(DOMAIN_ID)
    pipeline.publish_summary(DOMAIN_ID)
    dash = pipeline.dashboard()
    assert dash["分层计数"]["汇总层"] == 4
    assert dash["分层计数"]["汇总层已发布"] == 4
    overview = next(d for d in dash["主题域"] if d["编号"] == DOMAIN_ID)
    assert overview["汇总草稿行数"] == 4
    assert overview["已发布行数"] == 4
    assert overview["最近对账状态"] == "一致"
    assert overview["最近发布状态"] == "成功"
    assert dash["最近发布"][0]["状态"] == "成功"
    assert dash["最近发布"][0]["对账报告编号"] is not None


def test_layer_rows_isolated_between_states(fresh_db):
    build_to_draft()
    pipeline.run_reconcile(DOMAIN_ID)
    pipeline.publish_summary(DOMAIN_ID)
    draft_rows = pipeline.layer_rows("summary", DOMAIN_ID)
    published_rows = pipeline.layer_rows("summary_published", DOMAIN_ID)
    assert len(draft_rows) == 4 and len(published_rows) == 4
    # 发布副本带 publish_id，草稿没有——两表结构即隔离
    assert all("publish_id" in r for r in published_rows)
    assert all("publish_id" not in r for r in draft_rows)


def test_publish_evidence_recorded_and_readable_by_id(fresh_db):
    build_to_draft()
    report = pipeline.run_reconcile(DOMAIN_ID)
    rec = pipeline.publish_summary(DOMAIN_ID)
    assert rec["status"] == "成功"

    ev = one("SELECT * FROM publish_evidence WHERE publish_id=?", (rec["id"],))
    assert ev["reconcile_report_id"] == report["id"]
    assert ev["row_count"] == 4
    assert ev["success"] == 1
    assert ev["published_at"]

    detail = pipeline.publish_detail(rec["id"])
    assert detail["发布记录"]["id"] == rec["id"]
    assert detail["发布证明"]["row_count"] == 4
    assert detail["发布证明"]["reconcile_report_id"] == report["id"]
    assert len(detail["已发布行"]) == 4
    assert all(r["publish_id"] == rec["id"] for r in detail["已发布行"])

    # 已发布行数、证明行数与草稿快照同源
    assert detail["发布证明"]["row_count"] == one(
        "SELECT COUNT(*) AS c FROM layer_summary_published WHERE publish_id=?", (rec["id"],)
    )["c"]


def test_rejected_publish_has_evidence_without_published_rows(fresh_db):
    build_to_draft()
    rec = pipeline.publish_summary(DOMAIN_ID)
    assert rec["status"] == "拒绝"
    ev = one("SELECT * FROM publish_evidence WHERE publish_id=?", (rec["id"],))
    assert ev["success"] == 0
    assert ev["row_count"] == 0
    assert ev["reconcile_report_id"] is None
    assert "对账" in ev["reject_reason"]
    assert pipeline.publish_detail(rec["id"])["已发布行"] == []
    assert one("SELECT COUNT(*) AS c FROM layer_summary_published")["c"] == 0


def test_stale_success_does_not_survive_failed_reconcile(fresh_db):
    build_to_draft()
    pipeline.run_reconcile(DOMAIN_ID)
    first = pipeline.publish_summary(DOMAIN_ID)
    assert first["status"] == "成功"

    # 明细直接加行而不再汇总：生成新的差异对账后发布必须被拒，
    # 旧发布区不得被覆盖成半截，也不得回落到旧一致报告放行
    conn = db.connect()
    try:
        conn.execute(
            """
            INSERT INTO layer_detail(
              domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
            ) VALUES (?, '2026-11-12', 'S09', '城南店', '饮料', 10.0, 1, '已清洗')
            """,
            (DOMAIN_ID,),
        )
        conn.commit()
    finally:
        conn.close()
    diff = pipeline.run_reconcile(DOMAIN_ID)
    assert diff["status"] == "存在差异"
    rejected = pipeline.publish_summary(DOMAIN_ID)
    assert rejected["status"] == "拒绝"
    assert rejected["reconcile_report_id"] == diff["id"]
    # 已发布区仍是上一次成功发布的整份快照
    rows = q("SELECT * FROM layer_summary_published WHERE domain_id=?", (DOMAIN_ID,))
    assert len(rows) == 4
    assert {r["publish_id"] for r in rows} == {first["id"]}


def test_publish_write_failure_leaves_published_area_intact(fresh_db, monkeypatch):
    build_to_draft()
    pipeline.run_reconcile(DOMAIN_ID)
    first = pipeline.publish_summary(DOMAIN_ID)
    assert first["status"] == "成功"
    before = q("SELECT * FROM layer_summary_published WHERE domain_id=? ORDER BY id", (DOMAIN_ID,))

    # 让快照校验失败，模拟发布写入半途出错
    def boom(conn, publish_id, draft):
        raise RuntimeError("模拟写入中断")

    monkeypatch.setattr(pipeline, "_verify_snapshot", boom)
    rec = pipeline.publish_summary(DOMAIN_ID)
    assert rec["status"] == "拒绝"
    assert "整单回滚" in rec["reason"]
    after = q("SELECT * FROM layer_summary_published WHERE domain_id=? ORDER BY id", (DOMAIN_ID,))
    assert [(r["id"], r["publish_id"], r["store_code"]) for r in after] == [
        (r["id"], r["publish_id"], r["store_code"]) for r in before
    ]
    # 失败发布不残留半截批次
    assert one(
        "SELECT COUNT(*) AS c FROM layer_summary_published WHERE publish_id=?", (rec["id"],)
    )["c"] == 0


def test_overview_exposes_evidence_fields(fresh_db):
    build_to_draft()
    report = pipeline.run_reconcile(DOMAIN_ID)
    rec = pipeline.publish_summary(DOMAIN_ID)
    overview = next(d for d in pipeline.domain_publish_overview() if d["编号"] == DOMAIN_ID)
    assert overview["当前发布编号"] == rec["id"]
    assert overview["证明对账报告编号"] == report["id"]
    assert overview["证明行数"] == 4
    assert overview["证明发布区行数"] == 4
    assert overview["证明成败"] == "成功"
    assert overview["证明发布时间"]


def test_publish_detail_endpoint(fresh_db, monkeypatch):
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from app.main import app

    # 让应用启动时沿用测试库，而不是默认 DATA_DIR
    monkeypatch.setattr(seed, "init_db", lambda: None)
    with TestClient(app) as client:
        build_to_draft()
        pipeline.run_reconcile(DOMAIN_ID)
        rec = pipeline.publish_summary(DOMAIN_ID)
        res = client.get(f"/api/publish/{rec['id']}")
        assert res.status_code == 200
        body = res.json()
        assert body["发布证明"]["success"] == 1
        assert body["发布证明"]["row_count"] == 4
        assert client.get("/api/publish/999999").status_code == 404
