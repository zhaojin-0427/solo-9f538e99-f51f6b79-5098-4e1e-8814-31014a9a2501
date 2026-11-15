# -*- coding: utf-8 -*-
"""提交流程的业务裁决层。

裁决规则（全部在单条 BEGIN IMMEDIATE 事务内执行）：
  1. 同修订号已发布：
       内容哈希相同 -> 幂等重传，沿用原发布结果；
       内容哈希不同 -> 冲突拒绝，原发布不变。
  2. 修订号不高于当前已发布修订 -> 拒绝（仅较高修订可替换当前版）。
  3. 其余情况评估时间线：
       恰好一种可行解释 -> 发布为当前版（修订号严格更高时才替换）；
       零种/多种解释    -> 拒绝并说明原因，原发布不变。
"""

from __future__ import annotations

import hashlib
import json

from .csv_parser import parse_csv
from .db import (
    get_detail_rows,
    get_itinerary,
    get_latest_submission,
    get_published,
    get_published_segments,
    get_published_timeline_rows,
    get_revision,
    immediate_transaction,
    list_itineraries,
    log_submission,
    read_transaction,
    utcnow_iso,
)
from .timeline import Evaluation, evaluate, export_csv_lines, interpretation_to_rows

IDEMPOTENT = "idempotent_republish"
CONFLICT = "conflict"
STALE = "stale_revision"
PUBLISHED = "published"
REJECTED = "rejected"
STRUCTURAL = "structural"


def _hash_content(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _rejection_detail(ev: Evaluation) -> dict:
    if ev.status == "structural":
        return {"kind": "structural", "structural_errors": ev.structural_errors}
    if ev.status == "none":
        return {
            "kind": "no_interpretation",
            "message": "不存在满足全部约束的完整时间线（零种可行解释）",
            "structural_errors": ev.structural_errors,
            "rejection_reasons": ev.rejection_reasons,
        }
    # multiple
    return {
        "kind": "ambiguous",
        "message": f"存在 {len(ev.interpretations)} 种满足约束的解释，无法唯一确定时间线",
        "candidate_count": len(ev.interpretations),
        "candidates": [interpretation_to_rows(itp) for itp in ev.interpretations],
        "rejection_reasons": ev.rejection_reasons,
    }


def _persist_publish(conn, itinerary_id: int, revision_no: int, content_hash: str,
                     ev: Evaluation) -> int:
    itp = ev.interpretations[0]
    rows = interpretation_to_rows(itp)
    now = utcnow_iso()
    summary = {
        "status": PUBLISHED,
        "legs": len(rows),
        "first_depart_utc": rows[0]["depart_utc"],
        "last_arrive_utc": rows[-1]["arrive_utc"],
    }
    cur = conn.execute(
        "INSERT INTO revisions (itinerary_id, revision_no, status, content_sha256,"
        " result_summary, submitted_at) VALUES (?, ?, 'published', ?, ?, ?)",
        (itinerary_id, revision_no, content_hash,
         json.dumps(summary, ensure_ascii=False), now),
    )
    revision_id = cur.lastrowid
    for r in rows:
        conn.execute(
            "INSERT INTO published_segments (revision_id, seq, origin, destination,"
            " origin_tz, destination_tz, depart_local, arrive_local, depart_utc,"
            " arrive_utc, flight_minutes, transfer_minutes)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (revision_id, r["seq"], r["origin"], r["destination"], r["origin_tz"],
             r["destination_tz"], r["depart_local"], r["arrive_local"], r["depart_utc"],
             r["arrive_utc"], r["flight_minutes"], r["transfer_minutes"]),
    )
    conn.execute(
        "INSERT INTO published_versions (itinerary_id, revision_id, revision_no, published_at)"
        " VALUES (?, ?, ?, ?)"
        " ON CONFLICT(itinerary_id) DO UPDATE SET revision_id=excluded.revision_id,"
        " revision_no=excluded.revision_no, published_at=excluded.published_at",
        (itinerary_id, revision_id, revision_no, now),
    )
    return revision_id


def submit(itinerary_no: str, revision_no: int, raw: bytes, db_path: str | None = None) -> dict:
    """主入口：上传一份 CSV 并完成裁决，返回给页面展示的结果字典。"""
    if not itinerary_no or not itinerary_no.strip():
        return {"outcome": STRUCTURAL, "ok": False,
                "reasons": ["行程号不能为空"]}
    itinerary_no = itinerary_no.strip()
    if not isinstance(revision_no, int) or revision_no <= 0:
        return {"outcome": STRUCTURAL, "ok": False,
                "reasons": ["修订号必须为正整数"]}

    content_hash = _hash_content(raw)

    # 先在事务外做解析/评估（纯 CPU，不持锁，缩短关键区）
    try:
        parsed_rows = parse_csv(raw)
    except Exception as exc:  # TimelineError 等
        result = {"outcome": STRUCTURAL, "ok": False, "reasons": [str(exc)]}
        with immediate_transaction(db_path) as conn:
            log_submission(conn, itinerary_no, revision_no, content_hash, result)
        return result

    ev: Evaluation = evaluate(parsed_rows)

    with immediate_transaction(db_path) as conn:
        row = get_itinerary(conn, itinerary_no)
        if row is None:
            cur = conn.execute(
                "INSERT INTO itineraries (itinerary_no, created_at) VALUES (?, ?)",
                (itinerary_no, utcnow_iso()),
            )
            itinerary_id = cur.lastrowid
        else:
            itinerary_id = row["id"]

        existing = get_revision(conn, itinerary_id, revision_no)
        published = get_published(conn, itinerary_id)

        # 规则 1：同号且已发布的修订 -> 幂等重传或内容冲突（原发布不变）。
        # 同号但此前仅被拒绝的修订不占号，允许用新内容重新评估（见下方重试逻辑）。
        if existing is not None and existing["status"] == "published":
            if existing["content_sha256"] == content_hash:
                result = {
                    "outcome": IDEMPOTENT, "ok": True,
                    "revision_no": revision_no,
                    "message": "同修订号同内容重传，沿用原已发布结果",
                }
            else:
                result = {
                    "outcome": CONFLICT, "ok": False,
                    "revision_no": revision_no,
                    "message": f"修订号 {revision_no} 已发布且内容不同，冲突拒绝，原发布不变",
                }
            log_submission(conn, itinerary_no, revision_no, content_hash, result)
            return result

        # 同号此前被拒：移除旧拒绝记录，按本次内容重新评估
        if existing is not None and existing["status"] != "published":
            conn.execute("DELETE FROM revisions WHERE id = ?", (existing["id"],))

        # 规则 2：修订号必须高于当前已发布修订
        if published is not None and revision_no <= published["revision_no"]:
            result = {
                "outcome": STALE, "ok": False,
                "revision_no": revision_no,
                "current_revision": published["revision_no"],
                "message": f"修订号 {revision_no} 不高于当前已发布修订 "
                           f"{published['revision_no']}，仅较高修订可替换当前版",
            }
            conn.execute(
                "INSERT INTO revisions (itinerary_id, revision_no, status, content_sha256,"
                " result_summary, submitted_at) VALUES (?, ?, 'rejected', ?, ?, ?)",
                (itinerary_id, revision_no, content_hash,
                 json.dumps(result, ensure_ascii=False), utcnow_iso()),
            )
            log_submission(conn, itinerary_no, revision_no, content_hash, result)
            return result

        # 规则 3：时间线评估
        if ev.status == "unique":
            revision_id = _persist_publish(conn, itinerary_id, revision_no, content_hash, ev)
            result = {
                "outcome": PUBLISHED, "ok": True,
                "revision_no": revision_no, "revision_id": revision_id,
                "message": f"修订 {revision_no} 已发布：恰好一种可行解释，UTC 时间线已生成",
                "timeline": interpretation_to_rows(ev.interpretations[0]),
            }
        else:
            detail = _rejection_detail(ev)
            result = {
                "outcome": REJECTED, "ok": False,
                "revision_no": revision_no,
                "message": ("时间线存在结构/夏令时错误" if ev.status == "structural"
                            else ("零种可行解释" if ev.status == "none"
                                  else f"多种可行解释（{len(ev.interpretations)} 种），不予发布")),
                "detail": detail,
            }
            conn.execute(
                "INSERT INTO revisions (itinerary_id, revision_no, status, content_sha256,"
                " result_summary, submitted_at) VALUES (?, ?, 'rejected', ?, ?, ?)",
                (itinerary_id, revision_no, content_hash,
                 json.dumps(detail, ensure_ascii=False), utcnow_iso()),
            )
        log_submission(conn, itinerary_no, revision_no, content_hash, result)
        return result


def get_overview(db_path: str | None = None) -> list[dict]:
    # 纯读：一致性快照，不获取写锁，不阻塞其他行程的上传。
    with read_transaction(db_path) as conn:
        return [dict(r) for r in list_itineraries(conn)]


SEGMENT_FIELDS = (
    "seq", "origin", "destination", "origin_tz", "destination_tz",
    "depart_local", "arrive_local", "depart_utc", "arrive_utc",
    "flight_minutes", "transfer_minutes",
)


REVISION_FIELDS = ("revision_id", "revision_no", "status", "content_sha256", "submitted_at")


def _segments_from_view(rows) -> list[dict]:
    return [{key: row[key] for key in SEGMENT_FIELDS} for row in rows]


def get_detail(itinerary_no: str, latest_rev: int | None = None,
               db_path: str | None = None) -> dict | None:
    # 当前修订号、修订记录与 UTC 航段由同一条视图（同一次查询）返回：
    # 要么全部来自旧发布，要么全部来自新发布，不会混用版本。
    with read_transaction(db_path) as conn:
        rows = get_detail_rows(conn, itinerary_no)
        if not rows:
            return None
        head = rows[0]
        revision_rows = [r for r in rows if r["row_type"] == "revision"]
        segment_rows = [r for r in rows if r["row_type"] == "segment"]
        revisions = []
        for rev in revision_rows:
            entry = {key: rev[key] for key in REVISION_FIELDS}
            entry["id"] = rev["revision_id"]
            entry["is_current"] = rev["revision_id"] == head["current_revision_id"]
            revisions.append(entry)
        out = {
            "itinerary_no": itinerary_no,
            "created_at": head["created_at"],
            "current_revision": head["current_revision"],
            "published_at": head["published_at"],
            "current_revision_id": head["current_revision_id"],
            "timeline": _segments_from_view(segment_rows) or None,
            "revisions": revisions,
            "latest_submission": None,
        }
        if latest_rev is not None:
            sub = get_latest_submission(conn, itinerary_no, latest_rev)
            if sub is not None:
                out["latest_submission"] = {
                    "revision_no": sub["revision_no"],
                    "submitted_at": sub["submitted_at"],
                    "result": json.loads(sub["result"]),
                }
        return out


def get_published_timeline(itinerary_no: str, revision_no: int | None = None,
                           db_path: str | None = None) -> tuple[list[dict], int] | None:
    """下载用：只返回完整已发布版本。返回 (段列表, 修订号) 或 None。

    当前版通过单条视图查询取得「修订号 + 全部航段」，只会来自一个完整
    已发布版本；历史修订在同一读快照内校验发布状态后读取其航段。
    """
    with read_transaction(db_path) as conn:
        if revision_no is None:
            rows = get_published_timeline_rows(conn, itinerary_no)
            if not rows:
                return None
            return _segments_from_view(rows), rows[0]["current_revision"]
        row = get_itinerary(conn, itinerary_no)
        if row is None:
            return None
        rev = get_revision(conn, row["id"], revision_no)
        if rev is None or rev["status"] != "published":
            return None
        segments = [dict(r) for r in get_published_segments(conn, rev["id"])]
        if not segments:
            return None
        return segments, rev["revision_no"]


def export_published_csv(segments: list[dict]) -> list[list[str]]:
    header = [
        "seq", "origin", "destination", "origin_tz", "destination_tz",
        "depart_local", "arrive_local", "depart_utc", "arrive_utc",
        "flight_minutes", "transfer_minutes",
    ]
    lines = [header]
    for r in segments:
        lines.append([
            str(r["seq"]), r["origin"], r["destination"], r["origin_tz"], r["destination_tz"],
            r["depart_local"], r["arrive_local"], r["depart_utc"], r["arrive_utc"],
            str(r["flight_minutes"]),
            "" if r["transfer_minutes"] is None else str(r["transfer_minutes"]),
        ])
    return lines
