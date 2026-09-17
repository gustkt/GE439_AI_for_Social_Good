"""อ่าน config ทั้งหมดจาก environment / ไฟล์ .env ที่เดียว

กฎของโปรเจกต์นี้: ห้าม hardcode credential ในโค้ด ทุกอย่างมาจาก .env
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# ข้อความ log/print เป็นภาษาไทย - บน Windows ถ้า output ถูก redirect ลงไฟล์
# Python จะใช้ cp1252 แล้ว crash ตั้งแต่ startup จึงบังคับเป็น UTF-8 ไว้ก่อน
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

# override=False: ถ้าตั้ง env var ไว้จาก shell แล้ว ให้ shell ชนะไฟล์ .env
load_dotenv(BASE_DIR / ".env", override=False)


def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None or value.strip() == "" else value.strip()


def _float(name: str, default: float) -> float:
    try:
        return float(_str(name) or default)
    except ValueError:
        return default


def _int(name: str, default: int) -> int:
    try:
        return int(float(_str(name) or default))
    except ValueError:
        return default


def _opt_int(name: str) -> int | None:
    raw = _str(name)
    if not raw:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


# --- แหล่งข้อมูล -----------------------------------------------------------
# ใช้หลายแหล่งพร้อมกันได้ คั่นด้วยจุลภาค เช่น "blitzortung,xweather" (ดู app/sources/merged.py)
LIGHTNING_SOURCE = _str("LIGHTNING_SOURCE", "blitzortung,xweather").lower()

# --- Blitzortung (stream) ---------------------------------------------------
# เซิร์ฟเวอร์ WebSocket ของแผนที่ Blitzortung - ถ้าตัวแรกต่อไม่ได้จะวนไปตัวถัดไป
BLITZORTUNG_WS_HOSTS = [
    host.strip()
    for host in _str(
        "BLITZORTUNG_WS_HOSTS",
        "ws1.blitzortung.org,ws7.blitzortung.org,ws8.blitzortung.org",
    ).split(",")
    if host.strip()
]
# stream ไม่ส่ง frame มาเกินกี่วินาที = ถือว่าค้าง (feed ระดับโลกปกติมีข้อมูลเข้าทุกไม่กี่วินาที)
BLITZORTUNG_STALE_SECONDS = _int("BLITZORTUNG_STALE_SECONDS", 120)
# ตัดฟ้าผ่าซ้ำเมื่อใช้หลายแหล่งพร้อมกัน: เวลาต่างกันไม่เกิน / ระยะห่างไม่เกิน
MERGE_DEDUPE_SECONDS = _float("MERGE_DEDUPE_SECONDS", 1.0)
MERGE_DEDUPE_KM = _float("MERGE_DEDUPE_KM", 5.0)

# --- Ingest -----------------------------------------------------------------
# เก็บเฉพาะ strike ที่อยู่ในรัศมีนี้จากสนามใดสนามหนึ่ง (feed ของ Blitzortung เป็นระดับโลก)
INGEST_RADIUS_KM = float(os.getenv("INGEST_RADIUS_KM") or 30.0)

# --- Capture / Replay ---------------------------------------------------------
# CAPTURE=true -> บันทึก strike จริงที่ผ่านการกรองลง data/captures/*.jsonl
CAPTURE_ENABLED = (os.getenv("CAPTURE") or "").strip().lower() in {"1", "true", "yes"}
CAPTURE_DIR = BASE_DIR / "data" / "captures"
# LIGHTNING_SOURCE=replay ต้องระบุไฟล์ที่จะเล่นซ้ำ (ชื่อไฟล์ใน data/captures หรือ path เต็ม)
REPLAY_FILE = (os.getenv("REPLAY_FILE") or "").strip()
# ความเร็วในการเล่น: 1 = ตามเวลาจริง, 10 = เร็วขึ้น 10 เท่า
REPLAY_SPEED = float(os.getenv("REPLAY_SPEED") or 1.0)

# --- Xweather: เฝ้าเฉพาะสนามที่เลือก ------------------------------------------
#   "all"  = poll ทุกสนาม active ทุกรอบ (กิน quota มากที่สุด แต่ไม่พลาดพายุ)
#   "2,3"  = poll เฉพาะสนามที่เลือกทุกรอบ สนามอื่นแสดงเป็น UNMONITORED ไม่ใช่ ALL_CLEAR
#   ว่าง   = วนทีละกลุ่มต่อรอบ (ประหยัด quota แต่แต่ละสนามถูกเช็กทุก ~14 นาที)
_xweather_ids_raw = (os.getenv("XWEATHER_STADIUM_IDS") or "").replace(" ", "").lower()
XWEATHER_WATCH_ALL = _xweather_ids_raw == "all"
XWEATHER_STADIUM_IDS = [int(part) for part in _xweather_ids_raw.split(",") if part.isdigit()]

XWEATHER_CLIENT_ID = _str("XWEATHER_CLIENT_ID")
XWEATHER_CLIENT_SECRET = _str("XWEATHER_CLIENT_SECRET")
XWEATHER_BASE_URL = _str(
    "XWEATHER_BASE_URL", "https://data.api.xweather.com/lightning/closest"
)
# quota หมด / credential ผิด -> หยุดยิงแล้วลองใหม่ทุกกี่นาที (ไม่ยิงซ้ำรัว ๆ)
XWEATHER_RETRY_MINUTES = _int("XWEATHER_RETRY_MINUTES", 60)

# --- เรดาร์ฝน (RainViewer) ---------------------------------------------------
# เรดาร์ = สัญญาณเตือนล่วงหน้า (WATCH) ส่วนฟ้าผ่า = หลักฐานสั่งหยุด (ดู app/model/fusion.py)
RADAR_ENABLED = _str("RADAR_ENABLED", "true").lower() in {"1", "true", "yes"}
RADAR_MAPS_URL = _str("RADAR_MAPS_URL", "https://api.rainviewer.com/public/weather-maps.json")
# free tier ให้ zoom สูงสุด 7 (~1.2 กม./pixel ที่ละติจูดไทย)
RADAR_ZOOM = _int("RADAR_ZOOM", 7)
# ภาพใหม่ออกทุก 10 นาที เช็กทุก 5 นาทีพอ
RADAR_POLL_SECONDS = _int("RADAR_POLL_SECONDS", 300)
# 40 dBZ = เกณฑ์ที่ใช้กันทั่วไปเป็นตัวแทนฝนฟ้าคะนองแบบ convective (ค่าประมาณ)
RADAR_HOTSPOT_DBZ = _int("RADAR_HOTSPOT_DBZ", 40)
# cell ที่เล็กกว่านี้ (pixel) ถือเป็นสัญญาณรบกวน - 4 pixel ~ 6 ตร.กม.
RADAR_MIN_CELL_PIXELS = _int("RADAR_MIN_CELL_PIXELS", 4)
# รัศมีรอบสนามที่วิเคราะห์ (ใช้ติดตามการเคลื่อนที่ของฝนก่อนเข้าใกล้)
RADAR_SEARCH_KM = _float("RADAR_SEARCH_KM", 50.0)
# ภาพล่าสุดเก่ากว่านี้ = ข้อมูลเรดาร์ไม่พร้อม
RADAR_STALE_MINUTES = _int("RADAR_STALE_MINUTES", 30)
# เว้นระยะระหว่างคำขอ (free tier จำกัด 100 คำขอ/นาที)
RADAR_REQUEST_GAP_SECONDS = _float("RADAR_REQUEST_GAP_SECONDS", 0.7)
RADAR_RETENTION_MINUTES = _int("RADAR_RETENTION_MINUTES", 180)
# nowcast ของเรดาร์: ภาพทุก 10 นาที จึงใช้หน้าต่างยาวกว่าของฟ้าผ่า
RADAR_NOWCAST_WINDOW_MINUTES = _int("RADAR_NOWCAST_WINDOW_MINUTES", 40)
RADAR_NOWCAST_BIN_MINUTES = _int("RADAR_NOWCAST_BIN_MINUTES", 10)
RADAR_PALETTE_PATH = BASE_DIR / "data" / "rainviewer_colors.csv"

# --- การ poll --------------------------------------------------------------
POLL_INTERVAL_SECONDS = _int("POLL_INTERVAL_SECONDS", 60)
POLL_LIMIT = _int("POLL_LIMIT", 1000)
CLUSTER_MERGE_KM = _float("CLUSTER_MERGE_KM", 10.0)
POLL_CLUSTERS_PER_TICK = _int("POLL_CLUSTERS_PER_TICK", 0)

# --- วงรัศมีความปลอดภัย ----------------------------------------------------
# 13 กม. ~ 8 ไมล์ คือเกณฑ์ NCAA Lightning Safety สำหรับ "หยุดการแข่งขัน"
# และต้องเว้น 30 นาทีหลัง strike สุดท้ายจึงจะกลับมาเล่นได้
RING_DANGER_KM = _float("RING_DANGER_KM", 10.0)
RING_SUSPEND_KM = _float("RING_SUSPEND_KM", 13.0)
RING_WATCH_KM = _float("RING_WATCH_KM", 16.0)
ALL_CLEAR_MINUTES = _int("ALL_CLEAR_MINUTES", 30)

# --- Nowcast ---------------------------------------------------------------
NOWCAST_WINDOW_MINUTES = _int("NOWCAST_WINDOW_MINUTES", 20)
NOWCAST_BIN_MINUTES = _int("NOWCAST_BIN_MINUTES", 5)
NOWCAST_MAX_ETA_MINUTES = _float("NOWCAST_MAX_ETA_MINUTES", 60.0)
NOWCAST_MIN_SPEED_KMPM = _float("NOWCAST_MIN_SPEED_KMPM", 0.2)
# ความเร็วสูงสุดที่เชื่อได้ (กม./ชม.) - เกินนี้ถือว่าข้อมูลน้อยจน centroid กระโดด ไม่ใช่พายุจริง
NOWCAST_MAX_SPEED_KMPH = _float("NOWCAST_MAX_SPEED_KMPH", 120.0)

# --- เก็บ/ล้างข้อมูล -------------------------------------------------------
STRIKE_RETENTION_MINUTES = _int("STRIKE_RETENTION_MINUTES", 60)


# --- ที่เก็บไฟล์ -----------------------------------------------------------
_db_path = Path(_str("DB_PATH", "data/lightning.db"))
DB_PATH = _db_path if _db_path.is_absolute() else BASE_DIR / _db_path

GEOJSON_PATH = BASE_DIR / "data" / "thai_league1_2026-27_stadiums.geojson"
STATIC_DIR = BASE_DIR / "static"


def xweather_configured() -> bool:
    """Xweather ต้องใช้ 'คู่' client_id + client_secret ขาดอย่างใดอย่างหนึ่งไม่ได้"""
    return bool(XWEATHER_CLIENT_ID and XWEATHER_CLIENT_SECRET)
