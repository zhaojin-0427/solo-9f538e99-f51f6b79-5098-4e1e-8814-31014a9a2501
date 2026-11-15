# -*- coding: utf-8 -*-
"""航线时间线解析引擎。

职责：
  1. 用 zoneinfo 把各航段端点的「当地时间 + IANA 时区」解析为 UTC；
  2. 正确处理夏令时：
       - 春季跳变导致当地时刻不存在 -> 该解释无效；
       - 秋季回退导致当地时刻歧义   -> 展开为多种候选解释；
  3. 只接受同时满足下列条件的完整时间线：
       a. 每段实际飞行分钟数与计划飞行分钟数吻合；
       b. 相邻航段机场首尾相接（前段到达 == 后段出发）；
       c. 转机间隔在 30~180 分钟（含边界）；
  4. 枚举全部候选解释并按完整 UTC 时间戳去重，统计可行解释数量。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import product
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ---- 业务常量 -----------------------------------------------------------

MIN_TRANSFER_MINUTES = 30
MAX_TRANSFER_MINUTES = 180
MIN_LEGS = 2
MAX_LEGS = 5
DATETIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M", "%Y/%m/%d %H:%M")
LOCAL_DT_DISPLAY = "%Y-%m-%d %H:%M"
UTC_DISPLAY = "%Y-%m-%d %H:%M UTC"


class TimelineError(ValueError):
    """输入数据结构性错误（解析阶段即拒绝，不进入解释枚举）。"""


@dataclass(frozen=True)
class SegmentInput:
    seq: int
    origin: str
    destination: str
    origin_tz: str
    destination_tz: str
    depart_local: datetime          # naive datetime
    arrive_local: datetime          # naive datetime
    flight_minutes: int


@dataclass(frozen=True)
class ResolvedEndpoint:
    """一个端点（出发或到达）的解析结果。

    kind:
      unique      唯一时刻
      ambiguous   夏令时回退歧义，candidates 含两个 UTC 时刻
      nonexistent 夏令时跳变中不存在的当地时刻
    """

    airport: str
    tz_name: str
    local_naive: datetime
    kind: str
    candidates: tuple[datetime, ...]


@dataclass
class SegmentResult:
    seq: int
    origin: str
    destination: str
    origin_tz: str
    destination_tz: str
    depart_utc: datetime
    arrive_utc: datetime
    depart_local_text: str
    arrive_local_text: str
    flight_minutes: int
    transfer_minutes: int | None = None  # 本航段之前的转机间隔（第一段为 None）


@dataclass
class Interpretation:
    segments: list[SegmentResult]
    signature: tuple[datetime, ...]


@dataclass
class Evaluation:
    ok: bool
    status: str                    # unique | multiple | none | structural
    interpretations: list[Interpretation] = field(default_factory=list)
    structural_errors: list[str] = field(default_factory=list)
    # 被淘汰候选的原因（聚合，按原因文本去重后保留出现次数）
    rejection_reasons: list[dict] = field(default_factory=list)
    parsed: list[SegmentInput] = field(default_factory=list)


# ---- 基础工具 -----------------------------------------------------------

def parse_local_datetime(text: str, field_name: str) -> datetime:
    text = (text or "").strip()
    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    raise TimelineError(
        f"{field_name} 时间格式无法识别：{text!r}，"
        f"支持格式 YYYY-MM-DD HH:MM（或带 T、斜杠日期）"
    )


def resolve_endpoint(airport: str, tz_name: str, local_naive: datetime) -> ResolvedEndpoint:
    """把当地朴素时刻解析为 UTC 候选，并判定 DST 状态。"""
    try:
        tz = ZoneInfo(tz_name)
    except ZoneInfoNotFoundError:
        raise TimelineError(f"机场 {airport} 的 IANA 时区无效：{tz_name!r}")

    aware = local_naive.replace(tzinfo=tz)
    offset_before = tz.utcoffset(local_naive - timedelta(hours=2))
    offset_at = aware.utcoffset()
    offset_after = tz.utcoffset(local_naive + timedelta(hours=2))

    if offset_before == offset_after:
        # 前后偏移一致，处于稳定时区（普通时刻或整日不发生跳变）
        kind, candidates = "unique", (aware.astimezone(timezone.utc),)
    elif offset_after < offset_before:
        # 秋季回退：UTC 偏移变小，时钟向后拨，
        # [回退后时刻, 回退前时刻) 之间的当地时刻有两种解释
        if offset_at == offset_before:
            # fold=0 / fold=1 分别对应偏移较大（夏令时）与较小（标准时）
            early = local_naive.replace(tzinfo=tz, fold=0).astimezone(timezone.utc)
            late = local_naive.replace(tzinfo=tz, fold=1).astimezone(timezone.utc)
            kind, candidates = "ambiguous", (early, late)
        else:
            kind, candidates = "unique", (aware.astimezone(timezone.utc),)
    else:
        # 春季跳变：UTC 偏移变大，时钟向前拨，
        # [跳变前时刻, 跳变后时刻) 之间的当地时刻不存在
        if offset_at == offset_before:
            kind, candidates = "nonexistent", ()
        else:
            kind, candidates = "unique", (aware.astimezone(timezone.utc),)

    return ResolvedEndpoint(
        airport=airport,
        tz_name=tz_name,
        local_naive=local_naive,
        kind=kind,
        candidates=candidates,
    )


def _fmt_local(dt: datetime) -> str:
    return dt.strftime(LOCAL_DT_DISPLAY)


def _fmt_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime(UTC_DISPLAY)


# ---- 结构校验 -----------------------------------------------------------

def validate_structure(rows: list[SegmentInput]) -> list[str]:
    errors: list[str] = []
    if not (MIN_LEGS <= len(rows) <= MAX_LEGS):
        errors.append(f"航段数量必须为 {MIN_LEGS}~{MAX_LEGS} 个，实际为 {len(rows)} 个")
        return errors

    expected_seq = list(range(1, len(rows) + 1))
    actual_seq = [r.seq for r in rows]
    if actual_seq != expected_seq:
        errors.append(f"航段序号必须从 1 连续递增，实际序号为 {actual_seq}")

    for r in rows:
        if r.flight_minutes <= 0:
            errors.append(f"第 {r.seq} 段计划飞行分钟必须为正整数，实际为 {r.flight_minutes}")
        if r.depart_local >= r.arrive_local:
            errors.append(f"第 {r.seq} 段当地到达时间必须晚于当地出发时间")

    for prev, nxt in zip(rows, rows[1:]):
        if prev.destination != nxt.origin:
            errors.append(
                f"机场首尾相接校验失败：第 {prev.seq} 段到达 {prev.destination}，"
                f"第 {nxt.seq} 段出发为 {nxt.origin}"
            )
    return errors


# ---- 解释枚举 -----------------------------------------------------------

def _build_candidate(rows: list[SegmentInput], picks: tuple[ResolvedEndpoint, ...]) -> tuple[list[SegmentResult], list[str]]:
    """按给定端点选择构造一条候选时间线，并返回违规原因列表。"""
    endpoints = picks
    segments: list[SegmentResult] = []
    reasons: list[str] = []

    for i, row in enumerate(rows):
        dep = endpoints[i * 2]
        arr = endpoints[i * 2 + 1]
        # 由调用方保证传入的端点已选定具体 UTC 时刻（候选列表长度为 1）
        dep_utc = dep.candidates[0]
        arr_utc = arr.candidates[0]
        actual_flight = int(round((arr_utc - dep_utc).total_seconds() / 60))
        if actual_flight != row.flight_minutes:
            reasons.append(
                f"第 {row.seq} 段飞行分钟不吻合：计划 {row.flight_minutes} 分钟，"
                f"候选时间线实际 {actual_flight} 分钟"
            )
        segments.append(
            SegmentResult(
                seq=row.seq,
                origin=row.origin,
                destination=row.destination,
                origin_tz=row.origin_tz,
                destination_tz=row.destination_tz,
                depart_utc=dep_utc,
                arrive_utc=arr_utc,
                depart_local_text=_fmt_local(row.depart_local),
                arrive_local_text=_fmt_local(row.arrive_local),
                flight_minutes=row.flight_minutes,
            )
        )

    for i in range(1, len(segments)):
        gap = int(round((segments[i].depart_utc - segments[i - 1].arrive_utc).total_seconds() / 60))
        segments[i].transfer_minutes = gap
        if gap < MIN_TRANSFER_MINUTES or gap > MAX_TRANSFER_MINUTES:
            reasons.append(
                f"第 {i}→{i + 1} 段转机间隔 {gap} 分钟，"
                f"不在 {MIN_TRANSFER_MINUTES}~{MAX_TRANSFER_MINUTES} 分钟范围内"
            )

    return segments, reasons


def evaluate(rows: list[SegmentInput]) -> Evaluation:
    """主入口：结构校验 + DST 解释枚举 + 业务约束过滤。"""
    structural_errors = validate_structure(rows)
    if structural_errors:
        return Evaluation(ok=False, status="structural",
                          structural_errors=structural_errors, parsed=rows)

    # 解析所有端点
    endpoints: list[ResolvedEndpoint] = []
    for row in rows:
        endpoints.append(resolve_endpoint(row.origin, row.origin_tz, row.depart_local))
        endpoints.append(resolve_endpoint(row.destination, row.destination_tz, row.arrive_local))

    # 存在不存在的当地时刻 -> 枚举前先记录，且不产生任何完整解释
    nonexistent = [
        f"机场 {ep.airport}（{ep.tz_name}）当地时刻 {_fmt_local(ep.local_naive)} "
        f"落在夏令时跳变区间，该时刻不存在，解释无效"
        for ep in endpoints if ep.kind == "nonexistent"
    ]
    if nonexistent:
        return Evaluation(ok=False, status="none",
                          structural_errors=nonexistent, parsed=rows)

    option_lists = [list(ep.candidates) for ep in endpoints]
    valid: dict[tuple[datetime, ...], Interpretation] = {}
    reason_counter: dict[str, int] = {}

    for combo in product(*option_lists):
        picked: list[ResolvedEndpoint] = []
        for ep, chosen_utc in zip(endpoints, combo):
            picked.append(ResolvedEndpoint(
                airport=ep.airport, tz_name=ep.tz_name, local_naive=ep.local_naive,
                kind=ep.kind, candidates=(chosen_utc,),
            ))
        segments, reasons = _build_candidate(rows, tuple(picked))
        signature = tuple(
            ep_utc for seg in segments for ep_utc in (seg.depart_utc, seg.arrive_utc)
        )
        if not reasons:
            if signature not in valid:
                valid[signature] = Interpretation(segments=segments, signature=signature)
        else:
            for reason in reasons:
                reason_counter[reason] = reason_counter.get(reason, 0) + 1

    interpretations = list(valid.values())
    rejection_reasons = [
        {"reason": reason, "occurrences": count}
        for reason, count in sorted(reason_counter.items(), key=lambda kv: (-kv[1], kv[0]))
    ]

    if len(interpretations) == 1:
        return Evaluation(ok=True, status="unique",
                          interpretations=interpretations,
                          rejection_reasons=rejection_reasons, parsed=rows)
    if len(interpretations) > 1:
        return Evaluation(ok=False, status="multiple",
                          interpretations=interpretations,
                          rejection_reasons=rejection_reasons, parsed=rows)
    return Evaluation(ok=False, status="none",
                      rejection_reasons=rejection_reasons, parsed=rows)


# ---- 展示辅助 -----------------------------------------------------------

def interpretation_to_rows(itp: Interpretation) -> list[dict]:
    out = []
    for seg in itp.segments:
        out.append({
            "seq": seg.seq,
            "origin": seg.origin,
            "destination": seg.destination,
            "origin_tz": seg.origin_tz,
            "destination_tz": seg.destination_tz,
            "depart_local": seg.depart_local_text,
            "arrive_local": seg.arrive_local_text,
            "depart_utc": _fmt_utc(seg.depart_utc),
            "arrive_utc": _fmt_utc(seg.arrive_utc),
            "flight_minutes": seg.flight_minutes,
            "transfer_minutes": seg.transfer_minutes,
        })
    return out


def export_csv_lines(itp: Interpretation) -> list[list[str]]:
    header = [
        "seq", "origin", "destination", "origin_tz", "destination_tz",
        "depart_local", "arrive_local", "depart_utc", "arrive_utc",
        "flight_minutes", "transfer_minutes",
    ]
    lines = [header]
    for row in interpretation_to_rows(itp):
        lines.append([
            str(row["seq"]), row["origin"], row["destination"],
            row["origin_tz"], row["destination_tz"],
            row["depart_local"], row["arrive_local"],
            row["depart_utc"], row["arrive_utc"],
            str(row["flight_minutes"]),
            "" if row["transfer_minutes"] is None else str(row["transfer_minutes"]),
        ])
    return lines
