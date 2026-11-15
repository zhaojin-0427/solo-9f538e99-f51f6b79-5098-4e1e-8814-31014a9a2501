# -*- coding: utf-8 -*-
"""上传 CSV 解析。

要求列（支持中英文表头，顺序不限）：
  seq / 序号
  origin / 出发机场
  destination / 到达机场
  origin_tz / 出发时区
  destination_tz / 到达时区
  depart_local / 出发当地时间
  arrive_local / 到达当地时间
  flight_minutes / 计划飞行分钟
"""

from __future__ import annotations

import csv
import io
import re

from .timeline import (
    MAX_LEGS,
    MIN_LEGS,
    SegmentInput,
    TimelineError,
    parse_local_datetime,
)

HEADER_ALIASES = {
    "seq": {"seq", "序号", "航段序号", "段号"},
    "origin": {"origin", "出发机场", "起飞机场", "出发"},
    "destination": {"destination", "到达机场", "抵达机场", "到达"},
    "origin_tz": {"origin_tz", "出发时区", "起飞机场时区"},
    "destination_tz": {"destination_tz", "到达时区", "到达机场时区"},
    "depart_local": {"depart_local", "出发当地时间", "起飞当地时间", "出发时间"},
    "arrive_local": {"arrive_local", "到达当地时间", "抵达当地时间", "到达时间"},
    "flight_minutes": {"flight_minutes", "计划飞行分钟", "飞行分钟", "飞行时长分钟"},
}

AIRPORT_RE = re.compile(r"^[A-Z0-9]{3,4}$")
REQUIRED_KEYS = list(HEADER_ALIASES.keys())


def _decode(raw: bytes) -> str:
    for enc in ("utf-8-sig", "utf-8", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise TimelineError("文件编码无法识别，请使用 UTF-8 编码的 CSV")


def _map_headers(fieldnames: list[str]) -> dict[str, str]:
    """把实际表头映射为标准 key -> 实际列名。"""
    if not fieldnames:
        raise TimelineError("CSV 缺少表头行")
    lowered = { (name or "").strip().lower(): name for name in fieldnames }
    mapping: dict[str, str] = {}
    for key, aliases in HEADER_ALIASES.items():
        for alias in aliases:
            if alias.lower() in lowered:
                mapping[key] = lowered[alias.lower()]
                break
    missing = [k for k in REQUIRED_KEYS if k not in mapping]
    if missing:
        raise TimelineError(
            "CSV 缺少必需列：" + "、".join(missing)
            + f"。当前表头：{', '.join(fieldnames)}"
        )
    return mapping


def parse_csv(raw: bytes) -> list[SegmentInput]:
    text = _decode(raw)
    reader = csv.DictReader(io.StringIO(text))
    mapping = _map_headers(reader.fieldnames or [])

    rows: list[SegmentInput] = []
    seen_seq: set[int] = set()
    for line_no, raw_row in enumerate(reader, start=2):
        if raw_row is None:
            continue
        # 跳过整行空白
        if not any((v or "").strip() for v in raw_row.values()):
            continue

        def cell(key: str) -> str:
            return (raw_row.get(mapping[key]) or "").strip()

        try:
            seq = int(cell("seq"))
        except ValueError:
            raise TimelineError(f"第 {line_no} 行序号不是整数：{cell('seq')!r}")
        if seq in seen_seq:
            raise TimelineError(f"第 {line_no} 行序号 {seq} 重复")
        seen_seq.add(seq)

        origin = cell("origin").upper()
        destination = cell("destination").upper()
        origin_tz = cell("origin_tz")
        destination_tz = cell("destination_tz")
        if not AIRPORT_RE.match(origin):
            raise TimelineError(f"第 {line_no} 行出发机场代码无效：{cell('origin')!r}（需 3~4 位大写字母/数字）")
        if not AIRPORT_RE.match(destination):
            raise TimelineError(f"第 {line_no} 行到达机场代码无效：{cell('destination')!r}（需 3~4 位大写字母/数字）")
        if origin == destination:
            raise TimelineError(f"第 {line_no} 行出发与到达机场相同：{origin}")

        depart_local = parse_local_datetime(cell("depart_local"), f"第 {line_no} 行出发当地时间")
        arrive_local = parse_local_datetime(cell("arrive_local"), f"第 {line_no} 行到达当地时间")

        try:
            flight_minutes = int(cell("flight_minutes"))
        except ValueError:
            raise TimelineError(f"第 {line_no} 行计划飞行分钟不是整数：{cell('flight_minutes')!r}")

        rows.append(SegmentInput(
            seq=seq, origin=origin, destination=destination,
            origin_tz=origin_tz, destination_tz=destination_tz,
            depart_local=depart_local, arrive_local=arrive_local,
            flight_minutes=flight_minutes,
        ))

    if not (MIN_LEGS <= len(rows) <= MAX_LEGS):
        raise TimelineError(
            f"每份文件必须含 {MIN_LEGS}~{MAX_LEGS} 个航段，实际解析到 {len(rows)} 个航段"
        )
    rows.sort(key=lambda r: r.seq)
    return rows
