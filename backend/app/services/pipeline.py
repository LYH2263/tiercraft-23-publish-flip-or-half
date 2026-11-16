from __future__ import annotations

import json

from app.db import connect, rows_to_dicts


STEP_LABEL = {
    "paste_to_detail": "贴源到明细",
    "detail_to_summary": "明细到汇总",
}


def list_domains() -> list[dict]:
    conn = connect()
    try:
        return rows_to_dicts(conn.execute("SELECT * FROM domains ORDER BY id").fetchall())
    finally:
        conn.close()


def get_domain(domain_id: int) -> dict | None:
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM domains WHERE id=?", (domain_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def layer_rows(table: str, domain_id: int | None = None) -> list[dict]:
    # 草稿与已发布物理分表：summary 只查草稿，summary_published 只查发布副本，互不混读
    allowed = {
        "paste": "layer_paste",
        "detail": "layer_detail",
        "summary": "layer_summary",
        "summary_published": "layer_summary_published",
    }
    if table not in allowed:
        raise ValueError("未知分层")
    sql = f"SELECT * FROM {allowed[table]}"
    args: tuple = ()
    if domain_id is not None:
        sql += " WHERE domain_id=?"
        args = (domain_id,)
    sql += " ORDER BY id"
    conn = connect()
    try:
        return rows_to_dicts(conn.execute(sql, args).fetchall())
    finally:
        conn.close()


def layer_counts(domain_id: int | None = None) -> dict:
    conn = connect()
    try:
        where = " WHERE domain_id=?" if domain_id is not None else ""
        args = (domain_id,) if domain_id is not None else ()
        paste = conn.execute(f"SELECT COUNT(*) AS c FROM layer_paste{where}", args).fetchone()["c"]
        detail = conn.execute(f"SELECT COUNT(*) AS c FROM layer_detail{where}", args).fetchone()["c"]
        summary = conn.execute(f"SELECT COUNT(*) AS c FROM layer_summary{where}", args).fetchone()["c"]
        published = conn.execute(
            f"SELECT COUNT(*) AS c FROM layer_summary_published{where}", args
        ).fetchone()["c"]
        return {
            "贴源层": int(paste),
            "明细层": int(detail),
            "汇总层": int(summary),
            "汇总层已发布": int(published),
        }
    finally:
        conn.close()


def list_jobs(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM layer_jobs ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM layer_jobs WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["step_label"] = STEP_LABEL.get(d["step"], d["step"])
            out.append(d)
        return out
    finally:
        conn.close()


def run_paste_to_detail(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        conn.execute("DELETE FROM layer_detail WHERE domain_id=?", (domain_id,))
        paste = conn.execute(
            "SELECT * FROM layer_paste WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        out = drop = 0
        for row in paste:
            store_name = (row["store_name"] or "").strip()
            amount = float(row["amount"])
            if not store_name or amount < 0:
                drop += 1
                continue
            conn.execute(
                """
                INSERT INTO layer_detail(
                  domain_id, biz_day, store_code, store_name, category, amount, qty, clean_flag
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    domain_id,
                    row["biz_day"],
                    row["store_code"],
                    store_name,
                    row["category"],
                    amount,
                    int(row["qty"]),
                    "已清洗",
                ),
            )
            out += 1
        cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count)
            VALUES (?, 'paste_to_detail', '成功', ?, ?, ?, ?)
            """,
            (domain_id, f"清洗完成，剔除门店名为空或金额为负的行", len(paste), out, drop),
        )
        conn.commit()
        job_id = int(cur.lastrowid)
        return dict(conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def run_detail_to_summary(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        # 只重写草稿态汇总（layer_summary）；已发布区 layer_summary_published 不受本任务影响
        conn.execute("DELETE FROM layer_summary WHERE domain_id=?", (domain_id,))
        detail = conn.execute(
            "SELECT * FROM layer_detail WHERE domain_id=? ORDER BY id", (domain_id,)
        ).fetchall()
        buckets: dict[tuple, dict] = {}
        for row in detail:
            key = (row["biz_day"], row["store_code"])
            b = buckets.setdefault(
                key,
                {"biz_day": row["biz_day"], "store_code": row["store_code"], "amount": 0.0, "qty": 0, "n": 0},
            )
            b["amount"] += float(row["amount"])
            b["qty"] += int(row["qty"])
            b["n"] += 1
        for b in buckets.values():
            conn.execute(
                """
                INSERT INTO layer_summary(
                  domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (domain_id, b["biz_day"], b["store_code"], b["amount"], b["qty"], b["n"]),
            )
        cur = conn.execute(
            """
            INSERT INTO layer_jobs(domain_id, step, status, message, in_count, out_count, drop_count)
            VALUES (?, 'detail_to_summary', '成功', ?, ?, ?, 0)
            """,
            (domain_id, "按业务日+门店编码汇总，仅写入草稿；已发布区保持不变", len(detail), len(buckets)),
        )
        conn.commit()
        job_id = int(cur.lastrowid)
        return dict(conn.execute("SELECT * FROM layer_jobs WHERE id=?", (job_id,)).fetchone())
    finally:
        conn.close()


def run_reconcile(domain_id: int) -> dict:
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    try:
        paste_count = int(
            conn.execute("SELECT COUNT(*) AS c FROM layer_paste WHERE domain_id=?", (domain_id,)).fetchone()[
                "c"
            ]
        )
        detail_count = int(
            conn.execute(
                "SELECT COUNT(*) AS c FROM layer_detail WHERE domain_id=?", (domain_id,)
            ).fetchone()["c"]
        )
        summary = conn.execute(
            "SELECT COALESCE(SUM(line_count),0) AS s, COALESCE(SUM(amount_sum),0) AS a FROM layer_summary WHERE domain_id=?",
            (domain_id,),
        ).fetchone()
        summary_line_sum = int(summary["s"])
        summary_amount = float(summary["a"])
        detail_amount = float(
            conn.execute(
                "SELECT COALESCE(SUM(amount),0) AS a FROM layer_detail WHERE domain_id=?",
                (domain_id,),
            ).fetchone()["a"]
        )
        key_mismatch = 1 if summary_line_sum != detail_count else 0
        amount_drift = round(summary_amount - detail_amount, 4)
        status = "一致" if key_mismatch == 0 and abs(amount_drift) < 0.0001 else "存在差异"
        detail = {
            "贴源行数": paste_count,
            "明细行数": detail_count,
            "汇总行数合计": summary_line_sum,
            "明细金额合计": detail_amount,
            "汇总金额合计": summary_amount,
            "键对齐": key_mismatch == 0,
            "金额漂移": amount_drift,
        }
        cur = conn.execute(
            """
            INSERT INTO reconcile_reports(
              domain_id, paste_count, detail_count, summary_line_sum, key_mismatch, amount_drift, status, detail_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                domain_id,
                paste_count,
                detail_count,
                summary_line_sum,
                key_mismatch,
                amount_drift,
                status,
                json.dumps(detail, ensure_ascii=False),
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM reconcile_reports WHERE id=?", (int(cur.lastrowid),)).fetchone()
        d = dict(row)
        d["detail"] = json.loads(d.pop("detail_json"))
        return d
    finally:
        conn.close()


def list_reconcile(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM reconcile_reports ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC",
                (domain_id,),
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["detail"] = json.loads(d.pop("detail_json"))
            out.append(d)
        return out
    finally:
        conn.close()


def _publish_record(conn, record_id: int) -> dict:
    return dict(conn.execute("SELECT * FROM publish_records WHERE id=?", (record_id,)).fetchone())


def _publish_evidence(conn, publish_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM publish_evidence WHERE publish_id=?", (publish_id,)).fetchone()
    return dict(row) if row is not None else None


def _write_evidence(
    conn,
    publish_id: int,
    domain_id: int,
    report_id: int | None,
    row_count: int,
    success: bool,
    reject_reason: str,
) -> None:
    # 发布证明与发布记录同事务写入；成功时行数即本次写入已发布区的快照行数
    conn.execute(
        """
        INSERT INTO publish_evidence(
          publish_id, domain_id, reconcile_report_id, row_count, success, reject_reason
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (publish_id, domain_id, report_id, row_count, 1 if success else 0, reject_reason),
    )


def _record_publish_rejection(
    conn, domain_id: int, report_id: int | None, reason: str
) -> dict:
    cur = conn.execute(
        """
        INSERT INTO publish_records(domain_id, reconcile_report_id, row_count, status, reason)
        VALUES (?, ?, 0, '拒绝', ?)
        """,
        (domain_id, report_id, reason),
    )
    publish_id = int(cur.lastrowid)
    _write_evidence(conn, publish_id, domain_id, report_id, 0, False, reason)
    conn.commit()
    return _publish_record(conn, publish_id)


def _gate_violation(report) -> str | None:
    """发布闸口：最近一次层间对账必须存在且为「一致」，否则返回拒绝原因。"""
    if report is None:
        return "尚未进行层间对账，不能发布；请先对账并确认「一致」"
    if report["status"] != "一致":
        return (
            f"最近一次对账报告 {int(report['id'])} 状态为「{report['status']}」，"
            "未达「一致」，拒绝发布"
        )
    return None


def _verify_snapshot(conn, publish_id: int, draft: list) -> None:
    """同事务内校验已发布区内容与本次草稿快照逐行一致，防止半截发布。"""
    written = conn.execute(
        """
        SELECT * FROM layer_summary_published
        WHERE publish_id=? ORDER BY biz_day, store_code
        """,
        (publish_id,),
    ).fetchall()
    if len(written) != len(draft):
        raise RuntimeError(f"发布区写入行数 {len(written)} 与草稿快照 {len(draft)} 不符")
    for got, want in zip(written, draft):
        if (
            got["biz_day"] != want["biz_day"]
            or got["store_code"] != want["store_code"]
            or abs(float(got["amount_sum"]) - float(want["amount_sum"])) >= 0.0001
            or int(got["qty_sum"]) != int(want["qty_sum"])
            or int(got["line_count"]) != int(want["line_count"])
        ):
            raise RuntimeError("发布区内容与草稿快照不一致")


def publish_summary(domain_id: int) -> dict:
    """发布闸口：仅当该主题域最近一次层间对账为「一致」时，

    把当前草稿快照整体写入独立已发布区；否则整单拒绝并留痕原因。
    成功路径在单个事务内完成整区替换与校验，任何写入失败都整单回滚，发布区保持原样。
    """
    domain = get_domain(domain_id)
    if not domain:
        raise ValueError("主题域不存在")
    conn = connect()
    # 显式事务：发布闸口判定、草稿快照读取与已发布区整区替换必须在同一把写锁下完成，
    # 避免判定后草稿被并发重写，造成「对账一致」与实际发布内容脱节
    conn.isolation_level = None
    report_id: int | None = None
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            report = conn.execute(
                "SELECT * FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC LIMIT 1",
                (domain_id,),
            ).fetchone()
            report_id = int(report["id"]) if report is not None else None
            violation = _gate_violation(report)
            if violation:
                # 拒绝：发布区不动，只写拒绝记录与证明
                conn.rollback()
                return _record_publish_rejection(conn, domain_id, report_id, violation)

            draft = conn.execute(
                "SELECT * FROM layer_summary WHERE domain_id=? ORDER BY biz_day, store_code",
                (domain_id,),
            ).fetchall()
            cur = conn.execute(
                """
                INSERT INTO publish_records(domain_id, reconcile_report_id, row_count, status, reason)
                VALUES (?, ?, ?, '成功', '')
                """,
                (domain_id, report_id, len(draft)),
            )
            publish_id = int(cur.lastrowid)
            # 同一事务内整体替换已发布区：旧快照删除与新增快照落库要么全成、要么全退
            conn.execute("DELETE FROM layer_summary_published WHERE domain_id=?", (domain_id,))
            for row in draft:
                conn.execute(
                    """
                    INSERT INTO layer_summary_published(
                      publish_id, domain_id, biz_day, store_code, amount_sum, qty_sum, line_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        publish_id,
                        domain_id,
                        row["biz_day"],
                        row["store_code"],
                        float(row["amount_sum"]),
                        int(row["qty_sum"]),
                        int(row["line_count"]),
                    ),
                )
            _verify_snapshot(conn, publish_id, draft)
            _write_evidence(conn, publish_id, domain_id, report_id, len(draft), True, "")
            conn.commit()
            return _publish_record(conn, publish_id)
        except Exception as exc:  # noqa: BLE001 - 写入失败必须整单拒绝且不写发布区
            conn.rollback()
            reason = f"发布写入失败，已整单回滚，发布区保持不变：{exc}"
            return _record_publish_rejection(conn, domain_id, report_id, reason)
    finally:
        conn.close()


def publish_detail(publish_id: int) -> dict | None:
    """按发布记录 id 只读：发布记录 + 发布证明 + 该次发布的已发布行。"""
    conn = connect()
    try:
        record = conn.execute(
            "SELECT * FROM publish_records WHERE id=?", (publish_id,)
        ).fetchone()
        if record is None:
            return None
        rows = conn.execute(
            """
            SELECT * FROM layer_summary_published
            WHERE publish_id=? ORDER BY biz_day, store_code
            """,
            (publish_id,),
        ).fetchall()
        evidence = _publish_evidence(conn, publish_id)
        record_dict = dict(record)
        published_rows = rows_to_dicts(rows)
        return {
            # 中文键（界面直接消费）
            "发布记录": record_dict,
            "发布证明": evidence,
            "已发布行": published_rows,
            # 等价英文键，便于接口脚本按发布记录 id 读取
            "record": record_dict,
            "evidence": evidence,
            "published_rows": published_rows,
        }
    finally:
        conn.close()


def list_publish(domain_id: int | None = None) -> list[dict]:
    conn = connect()
    try:
        if domain_id is None:
            rows = conn.execute("SELECT * FROM publish_records ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM publish_records WHERE domain_id=? ORDER BY id DESC", (domain_id,)
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["evidence"] = _publish_evidence(conn, int(d["id"]))
            out.append(d)
        return out
    finally:
        conn.close()


def domain_publish_overview() -> list[dict]:
    """每个主题域的草稿/已发布/对账/发布近况，供总览区分双态并给出发布入口。"""
    conn = connect()
    try:
        out = []
        domains = conn.execute("SELECT * FROM domains ORDER BY id").fetchall()
        for d in domains:
            did = int(d["id"])
            draft_n = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM layer_summary WHERE domain_id=?", (did,)
                ).fetchone()["c"]
            )
            published_n = int(
                conn.execute(
                    "SELECT COUNT(*) AS c FROM layer_summary_published WHERE domain_id=?", (did,)
                ).fetchone()["c"]
            )
            report = conn.execute(
                "SELECT id, status FROM reconcile_reports WHERE domain_id=? ORDER BY id DESC LIMIT 1",
                (did,),
            ).fetchone()
            last_publish = conn.execute(
                "SELECT id, status, reason, reconcile_report_id, row_count, created_at"
                " FROM publish_records WHERE domain_id=? ORDER BY id DESC LIMIT 1",
                (did,),
            ).fetchone()
            # 已发布区的实际归属批次与证明字段同源派生：一律取自发布区表与发布证明表
            current = conn.execute(
                """
                SELECT pr.id AS publish_id, pr.reconcile_report_id, pr.created_at,
                       ev.published_at, ev.row_count AS evidence_row_count, ev.success,
                       (SELECT COUNT(*) FROM layer_summary_published lp
                          WHERE lp.publish_id = pr.id) AS area_row_count
                FROM layer_summary_published lp
                JOIN publish_records pr ON pr.id = lp.publish_id
                LEFT JOIN publish_evidence ev ON ev.publish_id = pr.id
                WHERE lp.domain_id = ?
                GROUP BY pr.id
                ORDER BY pr.id DESC
                LIMIT 1
                """,
                (did,),
            ).fetchone()
            out.append(
                {
                    "编号": did,
                    "名称": d["name"],
                    "业务键": d["biz_key"],
                    "汇总草稿行数": draft_n,
                    "已发布行数": published_n,
                    "最近对账状态": report["status"] if report else "未对账",
                    "最近对账报告编号": int(report["id"]) if report else None,
                    "最近发布状态": last_publish["status"] if last_publish else "未发布",
                    "发布拒绝原因": (last_publish["reason"] if last_publish else "") or "",
                    # 当前已发布快照的发布证明（无发布区时为空）
                    "当前发布编号": int(current["publish_id"]) if current else None,
                    "证明对账报告编号": (
                        int(current["reconcile_report_id"])
                        if current and current["reconcile_report_id"] is not None
                        else None
                    ),
                    "证明发布时间": (current["published_at"] or current["created_at"]) if current else None,
                    "证明行数": int(current["evidence_row_count"]) if current else 0,
                    "证明发布区行数": int(current["area_row_count"]) if current else 0,
                    "证明成败": (
                        ("成功" if int(current["success"]) == 1 else "拒绝") if current else "—"
                    ),
                }
            )
        return out
    finally:
        conn.close()


def dashboard() -> dict:
    domains = list_domains()
    counts = layer_counts()
    jobs = list_jobs()[:5]
    reports = list_reconcile()[:5]
    publishes = list_publish()[:5]
    return {
        "主题域数": len(domains),
        "分层计数": counts,
        "最近任务": [
            {
                "编号": j["id"],
                "步骤": STEP_LABEL.get(j["step"], j["step"]),
                "状态": j["status"],
                "写入": j["out_count"],
                "剔除": j["drop_count"],
            }
            for j in jobs
        ],
        "最近对账": [
            {
                "编号": r["id"],
                "状态": r["status"],
                "明细行数": r["detail_count"],
                "汇总行数合计": r["summary_line_sum"],
            }
            for r in reports
        ],
        "最近发布": [
            {
                "编号": p["id"],
                "主题域编号": p["domain_id"],
                "状态": p["status"],
                "对账报告编号": p["reconcile_report_id"],
                "行数": p["row_count"],
                "原因": p["reason"],
                "时间": p["created_at"],
                "证明发布时间": (p.get("evidence") or {}).get("published_at"),
                "证明行数": (p.get("evidence") or {}).get("row_count", 0),
                "证明成败": (
                    ("成功" if int(p["evidence"]["success"]) == 1 else "拒绝")
                    if p.get("evidence")
                    else "—"
                ),
            }
            for p in publishes
        ],
        "主题域": domain_publish_overview(),
    }
