# -*- coding: utf-8 -*-
"""只读路径并发测试。

覆盖修复目标：
  - 列表/详情/下载读取未结束时，其他行程的上传、以及同一行程较高修订
    的发布仍可完成（读不持有写锁）；
  - 详情页一次响应中的当前修订号、修订记录与 UTC 航段来自同一快照，
    并发更正后新请求看到新版，进行中的读取不混用新旧数据；
  - 下载只包含一个完整已发布版本。
"""

import os
import threading

import pytest

from app import service
from app.db import init_db

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")


def _sample(name: str) -> bytes:
    with open(os.path.join(SAMPLE_DIR, name), "rb") as fh:
        return fh.read()


VALID = _sample("sample1_valid_ambiguous_unique_R1.csv")   # 2 段
LEGS3 = _sample("sample4_valid_3legs_R1.csv")              # 3 段


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "reads.db")
    init_db(path)
    return path


def _pause_inside_read(monkeypatch, func_name, entered, release, capture=None):
    """让某个只读查询在事务内暂停，模拟慢读取。"""
    original = getattr(service, func_name)

    def slow(*args, **kwargs):
        result = original(*args, **kwargs)
        if capture is not None:
            capture.append(result)
        entered.set()
        assert release.wait(10), "测试未释放慢读取"
        return result

    monkeypatch.setattr(service, func_name, slow)


def _run_with_timeout(fn, timeout=3.0):
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # pragma: no cover - 诊断用
            box["error"] = exc

    thread = threading.Thread(target=target)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "操作在读取未结束时被阻塞"
    assert "error" not in box, box.get("error")
    return box.get("value")


def test_slow_detail_read_does_not_block_other_itinerary_upload(db_path, monkeypatch):
    service.submit("IT-SLOW", 1, VALID, db_path)
    entered, release = threading.Event(), threading.Event()
    _pause_inside_read(monkeypatch, "get_detail_rows", entered, release)

    reader = threading.Thread(
        target=lambda: service.get_detail("IT-SLOW", db_path=db_path)
    )
    reader.start()
    assert entered.wait(5)

    # 同一时刻另一人上传其他行程：不得被慢读取拖住
    res = _run_with_timeout(
        lambda: service.submit("IT-OTHER", 1, LEGS3, db_path), timeout=3.0
    )
    assert res["outcome"] == service.PUBLISHED

    release.set()
    reader.join(5)
    assert not reader.is_alive()


def test_slow_detail_read_does_not_block_higher_revision_publish(db_path, monkeypatch):
    service.submit("IT-SAME", 1, VALID, db_path)
    entered, release = threading.Event(), threading.Event()
    _pause_inside_read(monkeypatch, "get_detail_rows", entered, release)

    box = {}

    def read_detail():
        box["detail"] = service.get_detail("IT-SAME", db_path=db_path)

    reader = threading.Thread(target=read_detail)
    reader.start()
    assert entered.wait(5)

    res = _run_with_timeout(
        lambda: service.submit("IT-SAME", 2, LEGS3, db_path), timeout=3.0
    )
    assert res["outcome"] == service.PUBLISHED

    release.set()
    reader.join(5)
    assert not reader.is_alive()

    # 进行中的读取仍是一份自洽的旧发布：R1 的修订号与 R1 的 2 段
    old = box["detail"]
    assert old["current_revision"] == 1
    assert len(old["timeline"]) == 2
    published_revs = [r["revision_no"] for r in old["revisions"] if r["status"] == "published"]
    assert published_revs == [1]

    # 读取结束后发起的新请求看到新版 R2 的 3 段
    new = service.get_detail("IT-SAME", db_path=db_path)
    assert new["current_revision"] == 2
    assert len(new["timeline"]) == 3


def test_detail_response_never_mixes_old_and_new_versions(db_path, monkeypatch):
    """当前修订号、修订记录、UTC 航段必须来自同一快照。"""
    service.submit("IT-MIX", 1, VALID, db_path)
    entered, release = threading.Event(), threading.Event()
    _pause_inside_read(monkeypatch, "get_detail_rows", entered, release)

    box = {}
    reader = threading.Thread(
        target=lambda: box.update(detail=service.get_detail("IT-MIX", db_path=db_path))
    )
    reader.start()
    assert entered.wait(5)

    # 读取过程中替换当前版
    assert service.submit("IT-MIX", 2, LEGS3, db_path)["outcome"] == service.PUBLISHED

    release.set()
    reader.join(5)
    detail = box["detail"]

    # 修订记录里「当前已发布」的标记必须与 current_revision 一致，
    # 航段数量也必须对应同一修订，不能出现 R2 的号配 R1 的航段。
    current = detail["current_revision"]
    assert current == 1
    marked = [r["revision_no"] for r in detail["revisions"] if r.get("is_current")]
    assert marked == [current]
    assert len(detail["timeline"]) == 2


def test_slow_download_does_not_block_publish_and_stays_one_version(db_path, monkeypatch):
    service.submit("IT-DL", 1, VALID, db_path)
    entered, release = threading.Event(), threading.Event()
    _pause_inside_read(monkeypatch, "get_published_timeline_rows", entered, release)

    box = {}

    def download():
        box["found"] = service.get_published_timeline("IT-DL", db_path=db_path)

    reader = threading.Thread(target=download)
    reader.start()
    assert entered.wait(5)

    res = _run_with_timeout(
        lambda: service.submit("IT-DL", 2, LEGS3, db_path), timeout=3.0
    )
    assert res["outcome"] == service.PUBLISHED

    release.set()
    reader.join(5)

    # 进行中的下载只包含 R1 这一个完整已发布版本
    segments, rev_no = box["found"]
    assert rev_no == 1
    assert len(segments) == 2
    assert [s["seq"] for s in segments] == [1, 2]

    # 之后的新下载拿到 R2 的完整 3 段
    segments2, rev2 = service.get_published_timeline("IT-DL", db_path=db_path)
    assert rev2 == 2
    assert [s["seq"] for s in segments2] == [1, 2, 3]


def test_slow_list_read_does_not_block_upload(db_path, monkeypatch):
    service.submit("IT-LIST", 1, VALID, db_path)
    entered, release = threading.Event(), threading.Event()
    _pause_inside_read(monkeypatch, "list_itineraries", entered, release)

    reader = threading.Thread(target=lambda: service.get_overview(db_path=db_path))
    reader.start()
    assert entered.wait(5)

    res = _run_with_timeout(
        lambda: service.submit("IT-LIST-2", 1, LEGS3, db_path), timeout=3.0
    )
    assert res["outcome"] == service.PUBLISHED

    release.set()
    reader.join(5)
    assert not reader.is_alive()


def test_snapshot_age_is_bounded_by_request(db_path):
    """慢读取期间新请求仍能看到最新版本（不被旧快照缓存）。"""
    service.submit("IT-FRESH", 1, VALID, db_path)
    entered, release = threading.Event(), threading.Event()

    from app.db import read_transaction

    def long_read():
        with read_transaction(db_path) as conn:
            conn.execute("SELECT 1 FROM itineraries").fetchone()
            entered.set()
            assert release.wait(10)

    reader = threading.Thread(target=long_read)
    reader.start()
    assert entered.wait(5)

    from app import service as svc
    # 旧代码路径若在读取期间发布新版，新请求仍必须能立刻看到
    assert svc.submit("IT-FRESH", 2, LEGS3, db_path)["outcome"] == svc.PUBLISHED
    fresh = svc.get_detail("IT-FRESH", db_path=db_path)
    assert fresh["current_revision"] == 2
    assert len(fresh["timeline"]) == 3

    release.set()
    reader.join(5)


def test_read_routes_do_not_wait_for_writer(db_path, monkeypatch):
    """列表、详情、下载三个只读路由不获取写锁。

    有写事务正在持锁（上传裁决进行中）时，读路由仍能立即返回；
    这正是「另一人上传更正版被慢读取拖住」的反向场景。
    """
    from app import create_app
    from app.db import immediate_transaction

    service.submit("IT-RO", 1, VALID, db_path)
    monkeypatch.setenv("SCHED_DB_PATH", db_path)
    app = create_app()
    app.config.update(TESTING=True)
    client = app.test_client()

    results = {}

    def fetch(path):
        response = client.get(path)
        results[path] = response.status_code

    with immediate_transaction(db_path) as conn:
        conn.execute("SELECT 1").fetchone()  # 持有 RESERVED 写锁
        threads = [
            threading.Thread(target=fetch, args=(path,), daemon=True)
            for path in ("/", "/itinerary/IT-RO", "/download/IT-RO")
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        assert all(not t.is_alive() for t in threads), "只读路由被写锁阻塞"
        assert results == {
            "/": 200, "/itinerary/IT-RO": 200, "/download/IT-RO": 200,
        }

