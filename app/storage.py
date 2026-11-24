"""SQLite 存储层：一块表的一期数据。

表：
- config    单行：核账参数（每分钟上限、峰值、所报窗起点）
- readings  每分钟累计读数（同刻覆盖，按分钟时间戳唯一）
- audits    核账结论：kind=initial 只写一次，kind=current 每次更新
"""
from __future__ import annotations

import csv
import io
import sqlite3
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

from . import solver

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    per_minute_cap INTEGER,
    peak_wh INTEGER,
    reported_window TEXT
);
CREATE TABLE IF NOT EXISTS readings (
    minute_ts TEXT PRIMARY KEY,
    reading_wh INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK (kind IN ('initial', 'current')),
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    detail_json TEXT NOT NULL,
    created_ts TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audits_kind ON audits(kind);
"""

TS_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M",
              "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


class UploadError(ValueError):
    """CSV 整批拒绝。"""


def get_db(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    if conn.execute("SELECT COUNT(*) FROM config").fetchone()[0] == 0:
        conn.execute("INSERT INTO config(id) VALUES(1)")
        conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# CSV 解析
# --------------------------------------------------------------------------- #
def parse_csv(content: bytes) -> Dict[datetime, int]:
    """解析整分钟累计读数 CSV。同文件内同刻取最后一条。"""
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise UploadError(f"CSV 必须为 UTF-8 编码：{exc}") from exc
    reader = csv.reader(io.StringIO(text))
    rows: List[List[str]] = [r for r in reader if r and any(c.strip() for c in r)]
    if not rows:
        raise UploadError("CSV 为空")

    header = [c.strip().lower() for c in rows[0]]
    data_rows = rows[1:]
    if header[:2] not in (["timestamp", "reading"],
                          ["time", "reading"],
                          ["minute_ts", "reading_wh"]):
        # 无表头容错：首行也当数据
        if _parse_int(rows[0][1]) is None:
            raise UploadError("CSV 表头应为 timestamp,reading")
        data_rows = rows

    parsed: Dict[datetime, int] = {}
    for lineno, row in enumerate(data_rows, start=2):
        if len(row) < 2:
            raise UploadError(f"第 {lineno} 行列数不足，应为 timestamp,reading")
        ts = _parse_ts(row[0].strip(), lineno)
        val = _parse_int(row[1].strip())
        if val is None or val < 0:
            raise UploadError(f"第 {lineno} 行读数必须是非负整数 Wh")
        parsed[ts] = val  # 同刻覆盖
    if not parsed:
        raise UploadError("CSV 没有有效数据行")
    return parsed


def _parse_int(s: str) -> Optional[int]:
    try:
        if s.strip() != str(int(s.strip())):
            return None
        return int(s.strip())
    except (ValueError, TypeError):
        return None


def _parse_ts(s: str, lineno: int) -> datetime:
    for fmt in TS_FORMATS:
        try:
            dt = datetime.strptime(s, fmt)
            break
        except ValueError:
            continue
    else:
        raise UploadError(
            f"第 {lineno} 行时间 {s!r} 无法解析，格式示例 2026-11-01 08:00")
    if dt.second != 0 or dt.microsecond != 0:
        raise UploadError(f"第 {lineno} 行时间必须是整分钟")
    return dt


# --------------------------------------------------------------------------- #
# 合并上传
# --------------------------------------------------------------------------- #
def merge_upload(conn: sqlite3.Connection,
                 incoming: Dict[datetime, int]) -> Tuple[int, int]:
    """合并一批读数，全部校验通过才写库；否则抛 UploadError，库不变。

    返回 (合并后读数条数, 覆盖条数)。
    """
    existing = {datetime.strptime(r["minute_ts"], "%Y-%m-%d %H:%M"):
                r["reading_wh"]
                for r in conn.execute(
                    "SELECT minute_ts, reading_wh FROM readings")}
    overwritten = sum(1 for ts in incoming if ts in existing)
    merged = dict(existing)
    merged.update(incoming)
    ordered = sorted(merged.items())

    # 单调性冲突：指出冲突的相邻读数（用绝对时间戳，先于偏移量校验）
    for (t1, v1), (t2, v2) in zip(ordered, ordered[1:]):
        if v2 < v1:
            raise UploadError(
                "电表不回卷，发现冲突的相邻读数："
                f"{t1:%Y-%m-%d %H:%M} = {v1} Wh -> "
                f"{t2:%Y-%m-%d %H:%M} = {v2} Wh，整批拒绝")

    # 结构校验（与参数无关）
    first_ts, last_ts = ordered[0][0], ordered[-1][0]
    if (first_ts.minute % solver.WINDOW_MINUTES
            or last_ts.minute % solver.WINDOW_MINUTES):
        raise UploadError(
            "账期两端必须对齐 15 分钟边界："
            f"首读数 {first_ts:%Y-%m-%d %H:%M}、末读数 "
            f"{last_ts:%Y-%m-%d %H:%M}，整批拒绝")
    offsets = _to_offsets(ordered)
    try:
        solver.validate_period(offsets)
    except solver.SolverError as exc:
        raise UploadError(str(exc)) from exc

    # 若已配置每分钟上限，合并后还要满足段可行
    cap = conn.execute(
        "SELECT per_minute_cap FROM config").fetchone()["per_minute_cap"]
    if cap:
        offsets = _to_offsets(ordered)
        try:
            solver.build_pieces(offsets, cap,
                                (offsets[-1][0] - offsets[0][0])
                                // solver.WINDOW_MINUTES)
        except solver.SolverError as exc:
            raise UploadError(f"合并后实读无解：{exc}，整批拒绝") from exc

    with conn:
        conn.execute("DELETE FROM readings")
        conn.executemany(
            "INSERT INTO readings(minute_ts, reading_wh) VALUES(?, ?)",
            [(ts.strftime("%Y-%m-%d %H:%M"), v) for ts, v in ordered])
    return len(ordered), overwritten


def _to_offsets(ordered: List[Tuple[datetime, int]]
                ) -> List[Tuple[int, int]]:
    t0 = ordered[0][0]
    return [(int((ts - t0).total_seconds()) // 60, v)
            for ts, v in ordered]


def load_readings(conn: sqlite3.Connection
                  ) -> List[Tuple[datetime, int]]:
    rows = conn.execute(
        "SELECT minute_ts, reading_wh FROM readings ORDER BY minute_ts"
    ).fetchall()
    return [(datetime.strptime(r["minute_ts"], "%Y-%m-%d %H:%M"),
             r["reading_wh"]) for r in rows]


# --------------------------------------------------------------------------- #
# 配置与结论
# --------------------------------------------------------------------------- #
def save_config(conn: sqlite3.Connection, cap: Optional[int],
                peak: Optional[int], window_ts: Optional[str]) -> None:
    with conn:
        conn.execute(
            "UPDATE config SET per_minute_cap=?, peak_wh=?, "
            "reported_window=? WHERE id=1", (cap, peak, window_ts))


def get_config(conn: sqlite3.Connection) -> sqlite3.Row:
    return conn.execute("SELECT * FROM config WHERE id=1").fetchone()


def latest_audit(conn: sqlite3.Connection, kind: str
                 ) -> Optional[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM audits WHERE kind=? ORDER BY id DESC LIMIT 1",
        (kind,)).fetchone()


def has_initial(conn: sqlite3.Connection) -> bool:
    return latest_audit(conn, "initial") is not None


def add_audit(conn: sqlite3.Connection, kind: str, status: str,
              message: str, detail_json: str) -> None:
    with conn:
        conn.execute(
            "INSERT INTO audits(kind, status, message, detail_json, "
            "created_ts) VALUES(?, ?, ?, ?, ?)",
            (kind, status, message, detail_json,
             datetime.now().strftime("%Y-%m-%d %H:%M:%S")))


def reset_all(conn: sqlite3.Connection) -> None:
    with conn:
        conn.execute("DELETE FROM readings")
        conn.execute("DELETE FROM audits")
        conn.execute("UPDATE config SET per_minute_cap=NULL, "
                     "peak_wh=NULL, reported_window=NULL WHERE id=1")
