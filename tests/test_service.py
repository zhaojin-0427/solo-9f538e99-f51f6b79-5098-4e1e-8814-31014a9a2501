# -*- coding: utf-8 -*-
"""服务层裁决、修订规则与并发测试。"""

import os
import tempfile
import threading

import pytest

from app import service
from app.db import init_db

SAMPLE_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "samples")


def _sample(name: str) -> bytes:
    with open(os.path.join(SAMPLE_DIR, name), "rb") as fh:
        return fh.read()


@pytest.fixture()
def db_path(tmp_path):
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


VALID = _sample("sample1_valid_ambiguous_unique_R1.csv")
MULTI = _sample("sample2_two_interpretations_R1.csv")
GAP = _sample("sample3_nonexistent_time_R1.csv")
LEGS3 = _sample("sample4_valid_3legs_R1.csv")
BAD = _sample("sample5_constraint_violation_R1.csv")


# ---- 基础发布/拒绝 ------------------------------------------------------

def test_unique_interpretation_gets_published(db_path):
    res = service.submit("IT-1", 1, VALID, db_path)
    assert res["ok"] is True
    assert res["outcome"] == service.PUBLISHED
    detail = service.get_detail("IT-1", db_path=db_path)
    assert detail["current_revision"] == 1
    assert len(detail["timeline"]) == 2
    # UTC 列已落库
    assert detail["timeline"][0]["depart_utc"].endswith("UTC")


def test_multiple_interpretations_rejected_with_reasons(db_path):
    res = service.submit("IT-2", 1, MULTI, db_path)
    assert res["ok"] is False
    assert res["outcome"] == service.REJECTED
    assert res["detail"]["kind"] == "ambiguous"
    assert res["detail"]["candidate_count"] == 2
    detail = service.get_detail("IT-2", db_path=db_path)
    assert detail["current_revision"] is None  # 原发布不变（本就没有）


def test_nonexistent_time_rejected(db_path):
    res = service.submit("IT-3", 1, GAP, db_path)
    assert res["ok"] is False
    # 不存在的当地时刻 -> 零种解释，原因在 structural_errors 中说明
    assert res["detail"]["kind"] == "no_interpretation"
    assert any("不存在" in e for e in res["detail"]["structural_errors"])


def test_zero_valid_interpretation_lists_constraint_reasons(db_path):
    res = service.submit("IT-4", 1, BAD, db_path)
    assert res["ok"] is False
    reasons = " ".join(x["reason"] for x in res["detail"]["rejection_reasons"])
    assert "飞行分钟不吻合" in reasons
    assert "转机间隔" in reasons


# ---- 修订号规则 ---------------------------------------------------------

def test_higher_revision_replaces_current(db_path):
    service.submit("IT-5", 1, VALID, db_path)
    res2 = service.submit("IT-5", 2, LEGS3, db_path)
    assert res2["outcome"] == service.PUBLISHED
    detail = service.get_detail("IT-5", db_path=db_path)
    assert detail["current_revision"] == 2
    assert len(detail["timeline"]) == 3


def test_lower_or_equal_revision_rejected_and_published_unchanged(db_path):
    service.submit("IT-6", 2, VALID, db_path)
    res_low = service.submit("IT-6", 1, LEGS3, db_path)
    assert res_low["outcome"] == service.STALE
    # 同号 R2 且内容不同 -> 冲突（同号同内容才是幂等重传）
    res_eq = service.submit("IT-6", 2, LEGS3, db_path)
    assert res_eq["outcome"] == service.CONFLICT
    detail = service.get_detail("IT-6", db_path=db_path)
    assert detail["current_revision"] == 2
    assert len(detail["timeline"]) == 2  # 原发布不变：仍是 VALID 的 2 段


def test_same_revision_same_content_is_idempotent(db_path):
    first = service.submit("IT-7", 1, VALID, db_path)
    again = service.submit("IT-7", 1, VALID, db_path)
    assert again["outcome"] == service.IDEMPOTENT
    assert again["ok"] is True
    detail = service.get_detail("IT-7", db_path=db_path)
    assert detail["current_revision"] == 1
    # 修订记录仍只有一条
    assert len(detail["revisions"]) == 1


def test_same_revision_different_content_conflicts(db_path):
    service.submit("IT-8", 1, VALID, db_path)
    conflict = service.submit("IT-8", 1, LEGS3, db_path)
    assert conflict["outcome"] == service.CONFLICT
    assert conflict["ok"] is False
    detail = service.get_detail("IT-8", db_path=db_path)
    # 原发布不变：仍是 VALID 的 2 段
    assert detail["current_revision"] == 1
    assert len(detail["timeline"]) == 2


def test_rejected_revision_does_not_block_later_retry(db_path):
    # R1 因多解释被拒；同号换一份有效内容应可重新评估（此前未发布、未占号）
    service.submit("IT-9", 1, MULTI, db_path)
    res = service.submit("IT-9", 1, VALID, db_path)
    assert res["outcome"] == service.PUBLISHED


# ---- 下载只返回完整已发布版本 ------------------------------------------

def test_download_only_returns_published(db_path):
    service.submit("IT-10", 1, MULTI, db_path)  # 被拒
    assert service.get_published_timeline("IT-10", db_path=db_path) is None
    service.submit("IT-10", 2, VALID, db_path)  # 发布
    found = service.get_published_timeline("IT-10", db_path=db_path)
    assert found is not None
    segments, rev = found
    assert rev == 2 and len(segments) == 2
    # 显式请求一个不存在/未发布的修订 -> None
    assert service.get_published_timeline("IT-10", 1, db_path=db_path) is None
    assert service.get_published_timeline("IT-10", 99, db_path=db_path) is None
    assert service.get_published_timeline("NOPE", db_path=db_path) is None


# ---- 结构错误 -----------------------------------------------------------

def test_malformed_csv_recorded_and_rejected(db_path):
    res = service.submit("IT-11", 1, b"not,a,valid\n1,2,3\n", db_path)
    assert res["outcome"] == service.STRUCTURAL
    assert res["ok"] is False


def test_bad_revision_inputs(db_path):
    res = service.submit("IT-12", 0, VALID, db_path)
    assert res["outcome"] == service.STRUCTURAL
    res = service.submit("", 1, VALID, db_path)
    assert res["outcome"] == service.STRUCTURAL


# ---- 并发：按提交时最新修订裁决 ----------------------------------------

def test_concurrent_uploads_serialized_by_latest_revision(db_path):
    # 先发布 R1
    service.submit("IT-C", 1, VALID, db_path)
    outcomes = []

    def worker(rev, payload):
        outcomes.append((rev, service.submit("IT-C", rev, payload, db_path)["outcome"]))

    # 两个同内容 R2（先到发布、后到幂等），一个异内容 R2（先到则冲突、后到则过期），一个 R3
    threads = [
        threading.Thread(target=worker, args=(2, LEGS3)),
        threading.Thread(target=worker, args=(2, LEGS3)),
        threading.Thread(target=worker, args=(2, VALID)),
        threading.Thread(target=worker, args=(3, LEGS3)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    by_rev = {}
    for rev, outcome in outcomes:
        by_rev.setdefault(rev, []).append(outcome)

    # R3 修订号最高，必然发布并成为最终当前版
    assert service.PUBLISHED in by_rev[3]
    # R2 的裁决取决于各自提交时的最新修订：
    #   - 若某个 R2 先拿到写锁，则发布；其后同内容幂等、异内容冲突；
    #   - 若 R3 先发布，则所有 R2 都低于当前最新修订，全部判过期。
    assert set(by_rev[2]) <= {service.PUBLISHED, service.IDEMPOTENT,
                              service.CONFLICT, service.STALE}
    if service.PUBLISHED not in by_rev[2]:
        assert set(by_rev[2]) == {service.STALE}
    detail = service.get_detail("IT-C", db_path=db_path)
    assert detail["current_revision"] == 3


def test_concurrent_distinct_revisions_higher_wins(db_path):
    service.submit("IT-D", 1, VALID, db_path)
    outcomes = {}

    def worker(rev):
        outcomes[rev] = service.submit("IT-D", rev, LEGS3, db_path)["outcome"]

    threads = [threading.Thread(target=worker, args=(rev,)) for rev in (2, 3, 4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    # 最高修订 R4 必然发布并成为最终当前版；较低修订按各自提交时的最新修订
    # 裁决：抢锁早于更高修订时发布，否则判过期——结果只取决于实际提交顺序
    assert outcomes[4] == service.PUBLISHED
    assert all(v in (service.PUBLISHED, service.STALE) for v in outcomes.values())
    detail = service.get_detail("IT-D", db_path=db_path)
    assert detail["current_revision"] == 4
