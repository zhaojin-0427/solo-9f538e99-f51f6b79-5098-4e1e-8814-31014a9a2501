# -*- coding: utf-8 -*-
"""SQLite 持久化层。

并发模型：
  - 启用 WAL，配合 busy_timeout，多 worker/多请求可并发读取；
  - 所有「提交修订」流程在单条 BEGIN IMMEDIATE 事务内完成，
    SQLite 写锁把并发提交串行化，裁决依据的是拿到写锁那一刻的最新修订；
  - 所有只读路径（列表、详情、下载）使用 BEGIN DEFERRED 读事务，
    不获取写锁：读取未结束时并发上传/发布仍可完成；事务内所有查询
    看到同一 WAL 快照，不会混用新旧版本。
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS itineraries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    itinerary_no    TEXT NOT NULL UNIQUE,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    itinerary_id        INTEGER NOT NULL REFERENCES itineraries(id),
    revision_no         INTEGER NOT NULL,
    status              TEXT NOT NULL CHECK (status IN ('published','rejected','conflict')),
    content_sha256      TEXT NOT NULL,
    result_summary      TEXT NOT NULL,
    submitted_at        TEXT NOT NULL,
    UNIQUE (itinerary_id, revision_no)
);

CREATE TABLE IF NOT EXISTS published_versions (
    itinerary_id        INTEGER PRIMARY KEY REFERENCES itineraries(id),
    revision_id         INTEGER NOT NULL REFERENCES revisions(id),
    revision_no         INTEGER NOT NULL,
    published_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS published_segments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id         INTEGER NOT NULL REFERENCES revisions(id),
    seq                 INTEGER NOT NULL,
    origin              TEXT NOT NULL,
    destination         TEXT NOT NULL,
    origin_tz           TEXT NOT NULL,
    destination_tz      TEXT NOT NULL,
    depart_local        TEXT NOT NULL,
    arrive_local        TEXT NOT NULL,
    depart_utc          TEXT NOT NULL,
    arrive_utc          TEXT NOT NULL,
    flight_minutes      INTEGER NOT NULL,
    transfer_minutes    INTEGER
);

CREATE TABLE IF NOT EXISTS submissions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    itinerary_no    TEXT NOT NULL,
    revision_no     INTEGER NOT NULL,
    content_sha256  TEXT NOT NULL,
    result          TEXT NOT NULL,
    submitted_at    TEXT NOT NULL
);

-- 详情页/下载使用的一致性视图：行程 + 当前已发布修订 + 对应修订记录
-- 三者由同一次 JOIN 产生，不会出现「当前修订号指向另一条修订记录」的混用。
CREATE VIEW IF NOT EXISTS itinerary_current_publication AS
SELECT i.id                AS itinerary_id,
       i.itinerary_no      AS itinerary_no,
       i.created_at        AS created_at,
       pv.revision_id      AS revision_id,
       pv.revision_no      AS current_revision,
       pv.published_at     AS published_at,
       r.content_sha256    AS content_sha256,
       r.submitted_at      AS published_revision_submitted_at
FROM itineraries i
LEFT JOIN published_versions pv ON pv.itinerary_id = i.id
LEFT JOIN revisions r ON r.id = pv.revision_id;

-- 详情页/下载的唯一数据来源：一次 SELECT 同时给出
--   * 当前修订号（current_revision / current_revision_id）
--   * 修订记录（row_type='revision' 的行）
--   * 当前已发布版本的 UTC 航段（row_type='segment' 的行）
-- 三者由同一个视图、同一次查询产生，天然来自同一修订与同一快照。
CREATE VIEW IF NOT EXISTS itinerary_detail AS
SELECT icp.itinerary_id       AS itinerary_id,
       icp.itinerary_no       AS itinerary_no,
       icp.created_at         AS created_at,
       icp.current_revision   AS current_revision,
       icp.revision_id        AS current_revision_id,
       icp.published_at       AS published_at,
       'revision'             AS row_type,
       r.id                   AS revision_id,
       r.revision_no          AS revision_no,
       r.status               AS status,
       r.content_sha256       AS content_sha256,
       r.submitted_at         AS submitted_at,
       NULL                   AS seq,
       NULL                   AS origin,
       NULL                   AS destination,
       NULL                   AS origin_tz,
       NULL                   AS destination_tz,
       NULL                   AS depart_local,
       NULL                   AS arrive_local,
       NULL                   AS depart_utc,
       NULL                   AS arrive_utc,
       NULL                   AS flight_minutes,
       NULL                   AS transfer_minutes
FROM itinerary_current_publication icp
JOIN revisions r ON r.itinerary_id = icp.itinerary_id
UNION ALL
SELECT icp.itinerary_id, icp.itinerary_no, icp.created_at,
       icp.current_revision, icp.revision_id, icp.published_at,
       'segment',
       NULL, NULL, NULL, NULL, NULL,
       s.seq, s.origin, s.destination, s.origin_tz, s.destination_tz,
       s.depart_local, s.arrive_local, s.depart_utc, s.arrive_utc,
       s.flight_minutes, s.transfer_minutes
FROM itinerary_current_publication icp
JOIN published_segments s ON s.revision_id = icp.revision_id;
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db_path() -> str:
    return os.environ.get("SCHED_DB_PATH", os.path.join(os.getcwd(), "data", "schedule.db"))


def _connect(db_path: str | None = None, *, readonly: bool = False) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    # 先设 busy_timeout，任何后续 PRAGMA/查询遇到锁都会等待而非立即报错
    conn.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        # 只有写连接负责设定 journal_mode；读连接不去改动它，
        # 以免在写事务进行时产生额外的模式切换等待。
        conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: str | None = None) -> None:
    conn = _connect(db_path)
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


@contextmanager
def read_transaction(db_path: str | None = None):
    """一致性只读快照：BEGIN DEFERRED，只获取读锁。

    WAL 模式下读事务不阻塞写事务，因此长时间的列表/详情/下载读取
    不会拖住其他上传与发布。事务内所有查询共享同一快照：读取开始时
    已提交的版本贯穿整次读取，读取开始后提交的新版本只对后续请求可见。
    """
    conn = _connect(db_path, readonly=True)
    try:
        # 显式事务控制：避免 sqlite3 模块的隐式事务与 BEGIN DEFERRED 混淆
        conn.isolation_level = None
        conn.execute("PRAGMA query_only=ON")
        conn.execute("BEGIN DEFERRED")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@contextmanager
def immediate_transaction(db_path: str | None = None):
    """串行化写事务：BEGIN IMMEDIATE 立即获取 RESERVED 锁。"""
    conn = _connect(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---- 只读查询 -----------------------------------------------------------

def get_itinerary(conn: sqlite3.Connection, itinerary_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM itineraries WHERE itinerary_no = ?", (itinerary_no,)
    ).fetchone()


def get_revision(conn: sqlite3.Connection, itinerary_id: int, revision_no: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM revisions WHERE itinerary_id = ? AND revision_no = ?",
        (itinerary_id, revision_no),
    ).fetchone()


def get_published(conn: sqlite3.Connection, itinerary_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM published_versions WHERE itinerary_id = ?", (itinerary_id,)
    ).fetchone()


def list_revisions(conn: sqlite3.Connection, itinerary_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM revisions WHERE itinerary_id = ? ORDER BY revision_no", (itinerary_id,)
    ))


def get_current_publication(conn: sqlite3.Connection, itinerary_no: str) -> sqlite3.Row | None:
    """从一致性视图读取行程与其当前已发布修订（同一快照内的单条 JOIN）。"""
    return conn.execute(
        "SELECT * FROM itinerary_current_publication WHERE itinerary_no = ?",
        (itinerary_no,),
    ).fetchone()


def get_published_timeline_rows(conn: sqlite3.Connection,
                                itinerary_no: str) -> list[sqlite3.Row]:
    """下载用单条视图查询：当前修订号与其全部 UTC 航段来自同一行集合。"""
    return list(conn.execute(
        "SELECT * FROM itinerary_detail"
        " WHERE itinerary_no = ? AND row_type = 'segment' ORDER BY seq",
        (itinerary_no,),
    ))


def get_detail_rows(conn: sqlite3.Connection, itinerary_no: str) -> list[sqlite3.Row]:
    """详情页唯一数据源：修订号、修订记录与 UTC 航段取自同一条视图。"""
    return list(conn.execute(
        "SELECT * FROM itinerary_detail WHERE itinerary_no = ?"
        " ORDER BY CASE row_type WHEN 'revision' THEN 0 ELSE 1 END, revision_no, seq",
        (itinerary_no,),
    ))


def get_published_segments(conn: sqlite3.Connection, revision_id: int) -> list[sqlite3.Row]:
    return list(conn.execute(
        "SELECT * FROM published_segments WHERE revision_id = ? ORDER BY seq", (revision_id,)
    ))


def list_itineraries(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return list(conn.execute(
        """
        SELECT i.itinerary_no, i.created_at,
               pv.revision_no AS current_revision, pv.published_at
        FROM itineraries i
        LEFT JOIN published_versions pv ON pv.itinerary_id = i.id
        ORDER BY i.id DESC
        """
    ))


def log_submission(conn: sqlite3.Connection, itinerary_no: str, revision_no: int,
                   content_sha256: str, result: dict) -> None:
    conn.execute(
        "INSERT INTO submissions (itinerary_no, revision_no, content_sha256, result, submitted_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (itinerary_no, revision_no, content_sha256,
         json.dumps(result, ensure_ascii=False), utcnow_iso()),
    )


def get_latest_submission(conn: sqlite3.Connection, itinerary_no: str,
                          revision_no: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM submissions WHERE itinerary_no = ? AND revision_no = ?"
        " ORDER BY id DESC LIMIT 1",
        (itinerary_no, revision_no),
    ).fetchone()
