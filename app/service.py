"""核账服务：把库内读数与参数组装成 solver 输入，并持久化结论。"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from . import solver, storage
from .solver import WINDOW_MINUTES


class AuditInputError(ValueError):
    """参数不完整或非法（不产生结论，页面提示即可）。"""


def parse_window_ts(text: str) -> datetime:
    for fmt in storage.TS_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt)
        except ValueError:
            continue
    raise AuditInputError("所报时段格式示例：2026-11-01 08:15")


def _positive_int(text: str, label: str) -> int:
    try:
        v = int(str(text).strip())
    except (ValueError, AttributeError):
        raise AuditInputError(f"{label}必须是整数")
    if v <= 0:
        raise AuditInputError(f"{label}必须为正整数")
    return v


def run_audit(conn: sqlite3.Connection, cap_text: str, peak_text: str,
              window_text: str) -> solver.AuditResult:
    """保存参数并核对。成功才更新 current；initial 只写一次。"""
    cap = _positive_int(cap_text, "每分钟用电上限")
    peak_raw = int(str(peak_text).strip())
    if peak_raw < 0:
        raise AuditInputError("账单峰值必须是非负整数")
    win_start = parse_window_ts(window_text)
    if win_start.minute % WINDOW_MINUTES or win_start.second:
        raise AuditInputError("所报时段起点必须对齐 15 分钟边界")

    rows = storage.load_readings(conn)
    if len(rows) < 2:
        raise AuditInputError("请先上传至少两个读数")
    t0 = rows[0][0]
    t_end = rows[-1][0]
    if not (t0 <= win_start < t_end):
        raise AuditInputError("所报时段起点不在账期内")
    if (t_end - win_start).total_seconds() < WINDOW_MINUTES * 60:
        raise AuditInputError("所报时段必须完整落在账期内")

    offsets = [(int((ts - t0).total_seconds()) // 60, v) for ts, v in rows]
    target = int((win_start - t0).total_seconds()) // 60 // WINDOW_MINUTES

    # 先跑结构/上限校验，参数无法形成实读解时不覆盖当前结论
    try:
        result = solver.audit(offsets, cap, peak_raw, target)
    except solver.SolverError as exc:
        raise AuditInputError(f"当前实读无解：{exc}") from exc

    # 参数与结论在同一事务写入：任一失败整体回滚，不产生半成品状态
    detail = json.dumps(asdict(result), ensure_ascii=False)
    win_text = win_start.strftime("%Y-%m-%d %H:%M")
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    is_first = not storage.has_initial(conn)
    try:
        conn.execute("BEGIN")
        conn.execute(
            "UPDATE config SET per_minute_cap=?, peak_wh=?, "
            "reported_window=? WHERE id=1", (cap, peak_raw, win_text))
        conn.execute(
            "INSERT INTO audits(kind, status, message, detail_json, "
            "created_ts) VALUES('current', ?, ?, ?, ?)",
            (result.status, result.message, detail, now))
        if is_first:
            conn.execute(
                "INSERT INTO audits(kind, status, message, detail_json, "
                "created_ts) VALUES('initial', ?, ?, ?, ?)",
                (result.status, result.message, detail, now))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    return result


def window_rows(conn: sqlite3.Connection,
                result: Optional[solver.AuditResult] = None
                ) -> List[Tuple[str, int, int, bool]]:
    """把结论里的各窗电量还原为 (起点时间, 电量Wh, 窗索引, 是否所报窗)。"""
    rows = storage.load_readings(conn)
    if not rows or result is None or not result.window_totals:
        return []
    t0 = rows[0][0]
    out = []
    for i, wh in enumerate(result.window_totals):
        ts = t0 + timedelta(minutes=i * WINDOW_MINUTES)
        out.append((ts.strftime("%Y-%m-%d %H:%M"), wh, i,
                    i == result.target_window))
    return out
