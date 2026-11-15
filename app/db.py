# -*- coding: utf-8 -*-
"""SQLite 持久化层。

并发模型：
  - 启用 WAL，配合 busy_timeout，多 worker/多请求可并发读取；
  - 所有「提交修订」流程在单条 BEGIN IMMEDIATE 事务内完成，
    SQLite 写锁把并发提交串行化，裁决依据的是拿到写锁那一刻的最新修订。
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
"""


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_db_path() -> str:
    return os.environ.get("SCHED_DB_PATH", os.path.join(os.getcwd(), "data", "schedule.db"))


def _connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or get_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
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
