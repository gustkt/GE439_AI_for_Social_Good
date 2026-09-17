"""ชั้นเข้าถึงฐานข้อมูล SQLite

เจตนาของ MVP: เก็บ lat/lon เป็นคอลัมน์ REAL ธรรมดา แล้วคำนวณระยะทางใน Python
(ดู `app/geo.py`) - ไม่ใช้ PostGIS เพื่อให้ทั้งระบบรันได้ด้วย `pip install`
อย่างเดียว ไม่ต้องติดตั้ง database server

เปิด connection ใหม่ต่อหนึ่งคำสั่งเสมอ เพราะ API (threadpool) กับตัวรับข้อมูล
เรียกเข้ามาคนละ thread กัน ส่วน WAL mode ทำให้เขียนพร้อมกับอ่านได้โดยไม่ล็อกกัน
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager

from . import config
from .sources.base import Strike
from .timeutil import iso_minutes_ago, now_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS stadiums (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    club      TEXT    NOT NULL,
    name      TEXT    NOT NULL,
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    capacity  INTEGER,
    status    TEXT    NOT NULL DEFAULT 'current',
    note      TEXT    NOT NULL DEFAULT ''
);

-- กันการ seed ซ้ำ: หนึ่งสโมสรมีได้หลายสนาม (เช่น Pattani) และหนึ่งสนาม
-- ใช้ร่วมกันได้หลายสโมสร (True BG Stadium) แต่คู่ (club, name) ต้องไม่ซ้ำ
CREATE UNIQUE INDEX IF NOT EXISTS idx_stadiums_club_name
    ON stadiums (club, name);

CREATE TABLE IF NOT EXISTS strikes (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    stadium_id  INTEGER NOT NULL REFERENCES stadiums (id) ON DELETE CASCADE,
    ts_iso      TEXT    NOT NULL,   -- เวลาที่ฟ้าผ่าเกิดจริง (ไม่ใช่เวลารับข้อมูล)
    lat         REAL    NOT NULL,
    lon         REAL    NOT NULL,
    distance_km REAL    NOT NULL,   -- ระยะจากสนามนี้
    source      TEXT    NOT NULL,   -- blitzortung / xweather / replay:<ไฟล์>
    received_at TEXT    NOT NULL
);

-- query หลักคือ "strike ของสนาม X ใน N นาทีล่าสุด" - index นี้ตอบได้ตรง ๆ
CREATE INDEX IF NOT EXISTS idx_strikes_stadium_ts
    ON strikes (stadium_id, ts_iso);

-- กัน strike ซ้ำ (เช่น stream ส่งซ้ำหลัง reconnect หรือ poll ช่วงเวลาคาบเกี่ยว)
CREATE UNIQUE INDEX IF NOT EXISTS idx_strikes_dedupe
    ON strikes (stadium_id, ts_iso, lat, lon);

-- ผลวิเคราะห์ภาพเรดาร์ (ไม่เก็บภาพดิบ) หนึ่งแถวต่อหนึ่งภาพ
CREATE TABLE IF NOT EXISTS radar_frames (
    frame_ts    TEXT    PRIMARY KEY,   -- เวลาของภาพเรดาร์ (UTC)
    source      TEXT    NOT NULL,      -- rainviewer / replay:<ไฟล์>
    tiles_ok    INTEGER NOT NULL,
    tiles_total INTEGER NOT NULL,
    max_dbz     INTEGER,
    received_at TEXT    NOT NULL
);

-- cell ฝนแรง (กลุ่ม pixel >= เกณฑ์ dBZ ที่ติดกัน) ในแต่ละภาพ
CREATE TABLE IF NOT EXISTS radar_cells (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    frame_ts  TEXT    NOT NULL REFERENCES radar_frames (frame_ts) ON DELETE CASCADE,
    lat       REAL    NOT NULL,
    lon       REAL    NOT NULL,
    max_dbz   INTEGER NOT NULL,
    pixels    INTEGER NOT NULL,
    area_km2  REAL    NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_radar_cells_frame ON radar_cells (frame_ts);

-- ฝนแรงอยู่ใกล้แต่ละสนามแค่ไหน ในแต่ละภาพ
CREATE TABLE IF NOT EXISTS radar_exposure (
    frame_ts           TEXT    NOT NULL REFERENCES radar_frames (frame_ts) ON DELETE CASCADE,
    stadium_id         INTEGER NOT NULL REFERENCES stadiums (id) ON DELETE CASCADE,
    nearest_hotspot_km REAL,
    max_dbz_watch      INTEGER,
    activity_lat       REAL,
    activity_lon       REAL,
    activity_pixels    INTEGER NOT NULL,
    PRIMARY KEY (frame_ts, stadium_id)
);
"""

_init_lock = threading.Lock()


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    config.DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.DB_PATH, timeout=15.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db() -> None:
    with _init_lock, connect() as conn:
        # DB จากเวอร์ชันก่อนมีคอลัมน์ pulse_type แทน source - ตาราง strikes
        # เก็บแค่ข้อมูล 60 นาทีล่าสุดอยู่แล้ว จึงทิ้งแล้วสร้างใหม่ได้โดยไม่เสียอะไร
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(strikes)")}
        if columns and "source" not in columns:
            conn.execute("DROP TABLE strikes")
        conn.executescript(_SCHEMA)


# --- สนาม ------------------------------------------------------------------


def list_stadiums(active_only: bool = False) -> list[dict]:
    """คืนรายชื่อสนาม - `active_only` = เอาเฉพาะ status 'current'

    สนาม status='future' (เช่น Rainbow Stadium ที่ยังปรับปรุงอยู่) เก็บไว้ใน
    ฐานข้อมูลเพื่อให้เห็นภาพรวมลีก แต่จะไม่ถูกเฝ้าระวังและไม่ถูกคิดความเสี่ยง
    """
    sql = "SELECT * FROM stadiums"
    if active_only:
        sql += " WHERE status = 'current'"
    sql += " ORDER BY club, name"
    with connect() as conn:
        return [dict(row) for row in conn.execute(sql)]


def get_stadium(stadium_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM stadiums WHERE id = ?", (stadium_id,)).fetchone()
    return dict(row) if row else None


def upsert_stadium(
    club: str,
    name: str,
    lat: float,
    lon: float,
    capacity: int | None,
    status: str,
    note: str,
) -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO stadiums (club, name, lat, lon, capacity, status, note)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (club, name) DO UPDATE SET
                lat = excluded.lat,
                lon = excluded.lon,
                capacity = excluded.capacity,
                status = excluded.status,
                note = excluded.note
            """,
            (club, name, lat, lon, capacity, status, note),
        )


def count_stadiums() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM stadiums").fetchone()[0]


# --- strike ----------------------------------------------------------------


def insert_strikes(rows: list[tuple[int, Strike, float]]) -> int:
    """บันทึก strike ที่ผ่านการกรองแล้ว คืนจำนวนแถวที่เพิ่มจริง (ไม่นับที่ซ้ำ)

    rows = [(stadium_id, strike, distance_km_จากสนามนั้น), ...]
    strike หนึ่งครั้งอาจอยู่ใกล้หลายสนาม จึงเกิดได้หลายแถว
    """
    if not rows:
        return 0
    received_at = now_iso()
    values = [
        (stadium_id, s.ts_iso, s.lat, s.lon, distance, s.source, received_at)
        for stadium_id, s, distance in rows
    ]
    with connect() as conn:
        before = conn.total_changes
        conn.executemany(
            """
            INSERT OR IGNORE INTO strikes
                (stadium_id, ts_iso, lat, lon, distance_km, source, received_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        return conn.total_changes - before


def recent_strikes(stadium_id: int, minutes: float) -> list[dict]:
    """strike ของสนามนี้ใน N นาทีล่าสุด (ตามเวลาเกิดจริง) เรียงจากใหม่ไปเก่า"""
    since = iso_minutes_ago(minutes)
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT ts_iso, lat, lon, distance_km, source
            FROM strikes
            WHERE stadium_id = ? AND ts_iso >= ?
            ORDER BY ts_iso DESC
            """,
            (stadium_id, since),
        )
        return [dict(row) for row in rows]


def recent_strikes_bulk(minutes: float) -> dict[int, list[dict]]:
    """เหมือน recent_strikes แต่ดึงของทุกสนามในคำสั่งเดียว (กัน N+1 query)"""
    since = iso_minutes_ago(minutes)
    grouped: dict[int, list[dict]] = {}
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT stadium_id, ts_iso, lat, lon, distance_km, source
            FROM strikes
            WHERE ts_iso >= ?
            ORDER BY ts_iso DESC
            """,
            (since,),
        )
        for row in rows:
            item = dict(row)
            grouped.setdefault(item.pop("stadium_id"), []).append(item)
    return grouped


def cleanup_strikes(retention_minutes: int | None = None) -> int:
    """ลบ strike ที่เก่ากว่าช่วงเก็บรักษา (default 60 นาที)

    ระบบสนใจแค่ 30 นาทีล่าสุด (หน้าต่าง all-clear) การเก็บนานกว่านั้นทำให้
    ไฟล์ SQLite โตไปเรื่อย ๆ โดยไม่มีใครใช้ - dataset ระยะยาวให้ใช้ capture mode
    """
    minutes = retention_minutes or config.STRIKE_RETENTION_MINUTES
    cutoff = iso_minutes_ago(minutes)
    with connect() as conn:
        return conn.execute("DELETE FROM strikes WHERE ts_iso < ?", (cutoff,)).rowcount


def clear_strikes() -> None:
    """ล้าง strike ทั้งหมด - ใช้ตอนเริ่ม replay ไม่ให้ปนกับข้อมูลเดิม"""
    with connect() as conn:
        conn.execute("DELETE FROM strikes")


def count_strikes() -> int:
    with connect() as conn:
        return conn.execute("SELECT COUNT(*) FROM strikes").fetchone()[0]


# --- เรดาร์ ------------------------------------------------------------------


def radar_frame_exists(frame_ts: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM radar_frames WHERE frame_ts = ?", (frame_ts,)).fetchone() is not None


def store_radar_frame(frame) -> bool:
    """บันทึกผลวิเคราะห์ภาพเรดาร์หนึ่งภาพ - คืน False ถ้าภาพนี้เคยบันทึกแล้ว"""
    with connect() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO radar_frames
                (frame_ts, source, tiles_ok, tiles_total, max_dbz, received_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (frame.frame_ts, frame.source, frame.tiles_ok, frame.tiles_total, frame.max_dbz, now_iso()),
        )
        if cursor.rowcount == 0:
            return False
        conn.executemany(
            "INSERT INTO radar_cells (frame_ts, lat, lon, max_dbz, pixels, area_km2) VALUES (?, ?, ?, ?, ?, ?)",
            [(frame.frame_ts, c["lat"], c["lon"], c["max_dbz"], c["pixels"], c["area_km2"]) for c in frame.cells],
        )
        conn.executemany(
            """
            INSERT OR REPLACE INTO radar_exposure
                (frame_ts, stadium_id, nearest_hotspot_km, max_dbz_watch,
                 activity_lat, activity_lon, activity_pixels)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (frame.frame_ts, e["stadium_id"], e["nearest_hotspot_km"], e["max_dbz_watch"],
                 e["activity_lat"], e["activity_lon"], e["activity_pixels"])
                for e in frame.exposures
            ],
        )
        return True


def latest_radar_frame() -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM radar_frames ORDER BY frame_ts DESC LIMIT 1").fetchone()
    return dict(row) if row else None


def radar_cells_for(frame_ts: str) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            "SELECT lat, lon, max_dbz, pixels, area_km2 FROM radar_cells WHERE frame_ts = ? ORDER BY max_dbz DESC",
            (frame_ts,),
        )
        return [dict(row) for row in rows]


def radar_exposures_since(minutes: float) -> dict[int, list[dict]]:
    """ผลเรดาร์ของทุกสนามใน N นาทีล่าสุด เรียงจากภาพใหม่ไปเก่า"""
    since = iso_minutes_ago(minutes)
    grouped: dict[int, list[dict]] = {}
    with connect() as conn:
        rows = conn.execute(
            "SELECT * FROM radar_exposure WHERE frame_ts >= ? ORDER BY frame_ts DESC", (since,)
        )
        for row in rows:
            item = dict(row)
            grouped.setdefault(item["stadium_id"], []).append(item)
    return grouped


def cleanup_radar(retention_minutes: int | None = None) -> int:
    minutes = retention_minutes or config.RADAR_RETENTION_MINUTES
    with connect() as conn:
        return conn.execute(
            "DELETE FROM radar_frames WHERE frame_ts < ?", (iso_minutes_ago(minutes),)
        ).rowcount


def clear_radar() -> None:
    with connect() as conn:
        conn.execute("DELETE FROM radar_frames")
