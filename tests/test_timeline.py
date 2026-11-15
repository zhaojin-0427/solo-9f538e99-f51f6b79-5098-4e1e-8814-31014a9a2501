# -*- coding: utf-8 -*-
"""时间线引擎与 DST 处理测试。"""

from datetime import datetime

import pytest

from app.timeline import (
    MAX_TRANSFER_MINUTES,
    MIN_TRANSFER_MINUTES,
    SegmentInput,
    TimelineError,
    evaluate,
    resolve_endpoint,
)

NY = "America/New_York"
CHI = "America/Chicago"


def _seg(seq, o, d, dep, arr, mins, otz=NY, dtz=NY):
    return SegmentInput(
        seq=seq, origin=o, destination=d, origin_tz=otz, destination_tz=dtz,
        depart_local=datetime.strptime(dep, "%Y-%m-%d %H:%M"),
        arrive_local=datetime.strptime(arr, "%Y-%m-%d %H:%M"),
        flight_minutes=mins,
    )


# ---- DST 端点分类 -------------------------------------------------------

def test_ambiguous_fall_back_has_two_candidates():
    ep = resolve_endpoint("JFK", NY, datetime(2024, 11, 3, 1, 10))
    assert ep.kind == "ambiguous"
    assert len(ep.candidates) == 2
    # 夏令时解释（UTC-4）早于标准时解释（UTC-5）
    assert ep.candidates[0] < ep.candidates[1]


def test_nonexistent_spring_forward_has_no_candidate():
    ep = resolve_endpoint("JFK", NY, datetime(2024, 3, 10, 2, 30))
    assert ep.kind == "nonexistent"
    assert ep.candidates == ()


def test_normal_time_is_unique():
    ep = resolve_endpoint("JFK", NY, datetime(2024, 6, 15, 12, 0))
    assert ep.kind == "unique"
    assert len(ep.candidates) == 1


def test_post_fallback_time_is_unique():
    ep = resolve_endpoint("JFK", NY, datetime(2024, 11, 3, 3, 0))
    assert ep.kind == "unique"


def test_invalid_timezone_raises():
    with pytest.raises(TimelineError):
        resolve_endpoint("XXX", "Not/AZone", datetime(2024, 6, 1, 0, 0))


# ---- 结构校验 -----------------------------------------------------------

def test_bad_leg_count_rejected():
    rows = [_seg(1, "AAA", "BBB", "2024-06-01 08:00", "2024-06-01 09:00", 60)]
    ev = evaluate(rows)
    assert ev.status == "structural"
    assert any("航段数量" in e for e in ev.structural_errors)


def test_disconnected_airports_rejected():
    rows = [
        _seg(1, "AAA", "BBB", "2024-06-01 08:00", "2024-06-01 09:00", 60),
        _seg(2, "CCC", "DDD", "2024-06-01 10:00", "2024-06-01 11:00", 60),
    ]
    ev = evaluate(rows)
    assert ev.status == "structural"
    assert any("首尾相接" in e for e in ev.structural_errors)


def test_non_sequential_seq_rejected():
    rows = [
        _seg(2, "AAA", "BBB", "2024-06-01 08:00", "2024-06-01 09:00", 60),
        _seg(3, "BBB", "CCC", "2024-06-01 10:00", "2024-06-01 11:00", 60),
    ]
    ev = evaluate(rows)
    assert ev.status == "structural"
    assert any("序号" in e for e in ev.structural_errors)


# ---- 业务约束 -----------------------------------------------------------

def test_valid_two_legs_publishes_single_interpretation():
    rows = [
        _seg(1, "JFK", "DCA", "2024-06-01 08:00", "2024-06-01 09:30", 90),
        _seg(2, "DCA", "BOS", "2024-06-01 11:00", "2024-06-01 12:30", 90),
    ]
    ev = evaluate(rows)
    assert ev.status == "unique"
    assert len(ev.interpretations) == 1
    segs = ev.interpretations[0].segments
    assert segs[1].transfer_minutes == 90


def test_transfer_boundaries_are_inclusive():
    # 转机间隔恰好 30 与 180 分钟都应接受
    for gap in (MIN_TRANSFER_MINUTES, MAX_TRANSFER_MINUTES):
        rows = [
            _seg(1, "AAA", "BBB", "2024-06-01 08:00", "2024-06-01 09:00", 60),
            _seg(2, "BBB", "CCC", "2024-06-01 09:00",
                 "2024-06-01 10:00", 60),
        ]
        # 用第二段出发时间直接构造边界
        rows[1] = SegmentInput(
            seq=2, origin="BBB", destination="CCC", origin_tz=NY, destination_tz=NY,
            depart_local=rows[0].arrive_local.replace(),
            arrive_local=rows[0].arrive_local,
            flight_minutes=60,
        )
        # 重新手工构造更明确的边界数据
        from datetime import timedelta
        base = datetime(2024, 6, 1, 8, 0)
        rows = [
            _seg(1, "AAA", "BBB", "2024-06-01 08:00", "2024-06-01 09:00", 60),
            SegmentInput(
                seq=2, origin="BBB", destination="CCC", origin_tz=NY, destination_tz=NY,
                depart_local=base + timedelta(hours=1, minutes=gap),
                arrive_local=base + timedelta(hours=2, minutes=gap),
                flight_minutes=60,
            ),
        ]
        ev = evaluate(rows)
        assert ev.status == "unique", f"gap={gap} 应在边界内被接受"
        assert ev.interpretations[0].segments[1].transfer_minutes == gap


def test_transfer_out_of_range_rejected():
    rows = [
        _seg(1, "AAA", "BBB", "2024-06-01 08:00", "2024-06-01 09:00", 60),
        _seg(2, "BBB", "CCC", "2024-06-01 12:01", "2024-06-01 13:01", 60),
    ]
    ev = evaluate(rows)
    assert ev.status == "none"
    assert any("转机间隔" in x["reason"] for x in ev.rejection_reasons)


def test_flight_minutes_mismatch_rejected():
    rows = [
        _seg(1, "AAA", "BBB", "2024-06-01 08:00", "2024-06-01 09:00", 90),  # 实际 60
        _seg(2, "BBB", "CCC", "2024-06-01 10:00", "2024-06-01 11:00", 60),
    ]
    ev = evaluate(rows)
    assert ev.status == "none"
    assert any("飞行分钟不吻合" in x["reason"] for x in ev.rejection_reasons)


# ---- DST 场景 -----------------------------------------------------------

def test_ambiguous_endpoint_single_surviving_interpretation():
    # 01:10 歧义；只有 fold=0（EDT）解释能满足 140 分钟飞行
    rows = [
        _seg(1, "JFK", "DCA", "2024-11-03 01:10", "2024-11-03 02:30", 140),
        _seg(2, "DCA", "BOS", "2024-11-03 03:40", "2024-11-03 05:00", 80),
    ]
    ev = evaluate(rows)
    assert ev.status == "unique"
    dep = ev.interpretations[0].segments[0].depart_utc
    assert dep.strftime("%H:%M") == "05:10"  # EDT 解释


def test_two_valid_interpretations_counted():
    rows = [
        _seg(1, "JFK", "BOS", "2024-11-03 01:10", "2024-11-03 01:40", 30),
        _seg(2, "BOS", "DCA", "2024-11-03 03:10", "2024-11-03 04:40", 90),
    ]
    ev = evaluate(rows)
    assert ev.status == "multiple"
    assert len(ev.interpretations) == 2


def test_nonexistent_local_time_yields_zero():
    rows = [
        _seg(1, "JFK", "DCA", "2024-03-10 02:30", "2024-03-10 03:45", 75),
        _seg(2, "DCA", "BOS", "2024-03-10 05:00", "2024-03-10 06:20", 80),
    ]
    ev = evaluate(rows)
    assert ev.status == "none"
    assert any("不存在" in e for e in ev.structural_errors)


def test_cross_timezone_utc_computation():
    rows = [
        _seg(1, "JFK", "ORD", "2024-06-03 08:00", "2024-06-03 09:30", 150, NY, CHI),
        _seg(2, "ORD", "DEN", "2024-06-03 11:00", "2024-06-03 14:30", 270, CHI, "America/Denver"),
    ]
    ev = evaluate(rows)
    assert ev.status == "unique"
    segs = ev.interpretations[0].segments
    assert segs[0].depart_utc.strftime("%H:%M") == "12:00"
    assert segs[1].transfer_minutes == 90
