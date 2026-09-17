"""จุดรับข้อมูลกลาง: กรอง strike ตามระยะจากสนาม แล้วเขียนลง SQLite ที่เดียว

ทุกแหล่งข้อมูลส่ง strike มาที่ `Ingestor.__call__` เหมือนกันหมด
    แหล่งข้อมูล --(list[Strike])--> Ingestor --(เฉพาะที่ใกล้สนาม)--> SQLite
                                        └---(ถ้าเปิด capture)--> data/captures/*.jsonl

ทำไมต้องกรองก่อนเก็บ: feed ของ Blitzortung เป็นระดับโลก วินาทีละหลายสิบ strike
ถ้าเก็บทั้งหมด ฐานข้อมูลจะโตเร็วมากทั้งที่ระบบสนใจแค่รอบสนามไทย 16 แห่ง
"""

from __future__ import annotations

import json
import logging
import threading
from collections import deque
from pathlib import Path

from . import config, db
from .geo import haversine_km
from .sources.base import Strike
from .timeutil import iso_minutes_ago, now_iso, utcnow

log = logging.getLogger(__name__)

# 1 องศาละติจูด ~111 กม. ใช้ทำ bounding box คร่าว ๆ ก่อนคำนวณ haversine จริง
_KM_PER_DEG_LAT = 111.0


class Ingestor:
    def __init__(self, capture: bool | None = None) -> None:
        self._lock = threading.Lock()
        self._stadiums: list[dict] = []
        self._bbox: tuple[float, float, float, float] | None = None
        self.received_total = 0
        self.stored_total = 0
        self.last_strike_ts: str | None = None
        self.last_stored_at: str | None = None

        # ตัวนับสำหรับวินิจฉัย: แยกให้เห็นว่า "แหล่งข้อมูลไม่ส่ง strike ในไทยมาเลย"
        # หรือ "ส่งมาแต่ไกลสนามเกินรัศมีที่เก็บ"
        self.last_received_ts: str | None = None
        self.in_region_total = 0
        self.last_in_region_ts: str | None = None
        self.nearest_km: float | None = None
        self.nearest_stadium: str | None = None
        # ฟ้าผ่าจริงทั่วพื้นที่สนามไทย (ก่อนกรองระยะ) เก็บในหน่วยความจำไว้แสดงบนแผนที่
        # ให้ผู้ใช้เห็นว่าข้อมูลสดไหลเข้าจริง แม้ยังไม่มีพายุเข้าใกล้สนามไหนเลย
        self._region_recent: deque[dict] = deque(maxlen=5000)

        # กัน strike ซ้ำในไฟล์ capture: การ poll แต่ละรอบได้ข้อมูลย้อนหลังซ้อนกัน
        # (Xweather ย้อนหลัง 5 นาที) ฐานข้อมูลมี unique index กรองให้อยู่แล้ว
        # แต่ไฟล์ JSONL เป็นการ append ล้วน จึงต้องกรองเองตรงนี้
        self._captured_keys: set[tuple] = set()
        self.radar_frames_stored = 0
        self.last_radar_frame_ts: str | None = None

        enabled = config.CAPTURE_ENABLED if capture is None else capture
        self.capture_path: Path | None = None
        if enabled:
            config.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
            self.capture_path = config.CAPTURE_DIR / f"capture_{stamp}.jsonl"
            log.info("เปิด capture mode: บันทึก strike จริงลง %s", self.capture_path)

        self.refresh_stadiums()

    def refresh_stadiums(self) -> None:
        """โหลดสนาม active และคำนวณกรอบสี่เหลี่ยมที่ครอบทุกสนาม + รัศมี"""
        stadiums = db.list_stadiums(active_only=True)
        radius_deg = config.INGEST_RADIUS_KM / _KM_PER_DEG_LAT * 1.5  # เผื่อที่ละติจูดต่ำ
        with self._lock:
            self._stadiums = stadiums
            if stadiums:
                self._bbox = (
                    min(s["lat"] for s in stadiums) - radius_deg,
                    max(s["lat"] for s in stadiums) + radius_deg,
                    min(s["lon"] for s in stadiums) - radius_deg,
                    max(s["lon"] for s in stadiums) + radius_deg,
                )

    def _distance(self, stadium: dict, strike: Strike) -> float:
        # ใช้ระยะที่ผู้ให้บริการคำนวณให้ (เช่น Xweather relativeTo.distanceKM)
        # เฉพาะเมื่อจุดอ้างอิงคือตัวสนามนี้เอง ไม่งั้นค่านั้นวัดจากคนละจุด
        if (
            strike.ref_distance_km is not None
            and strike.ref_lat is not None
            and strike.ref_lon is not None
            and abs(strike.ref_lat - stadium["lat"]) < 1e-6
            and abs(strike.ref_lon - stadium["lon"]) < 1e-6
        ):
            return strike.ref_distance_km
        return haversine_km(stadium["lat"], stadium["lon"], strike.lat, strike.lon)

    def __call__(self, strikes: list[Strike]) -> int:
        """callback กลาง - คืนจำนวนแถวที่บันทึกลง DB จริง"""
        if not strikes:
            return 0

        with self._lock:
            stadiums = self._stadiums
            bbox = self._bbox
        self.received_total += len(strikes)
        self.last_received_ts = max(s.ts_iso for s in strikes)

        rows: list[tuple[int, Strike, float]] = []
        nearby: list[Strike] = []
        for strike in strikes:
            if bbox and not (bbox[0] <= strike.lat <= bbox[1] and bbox[2] <= strike.lon <= bbox[3]):
                continue  # ไกลจากประเทศไทยแน่นอน ไม่ต้องคำนวณต่อ
            self.in_region_total += 1
            self.last_in_region_ts = strike.ts_iso
            self._region_recent.append(
                {"ts_iso": strike.ts_iso, "lat": strike.lat, "lon": strike.lon, "source": strike.source}
            )
            matched = False
            for stadium in stadiums:
                distance = self._distance(stadium, strike)
                if self.nearest_km is None or distance < self.nearest_km:
                    self.nearest_km = round(distance, 1)
                    self.nearest_stadium = stadium["name"]
                if distance <= config.INGEST_RADIUS_KM:
                    rows.append((stadium["id"], strike, round(distance, 3)))
                    matched = True
            if matched:
                nearby.append(strike)

        if not rows:
            return 0

        stored = db.insert_strikes(rows)
        self.stored_total += stored
        self.last_stored_at = now_iso()
        self.last_strike_ts = max(s.ts_iso for s in nearby)
        if self.capture_path is not None:
            fresh = [s for s in nearby if self._is_new_for_capture(s)]
            if fresh:
                self._capture(fresh)
        return stored

    def _is_new_for_capture(self, strike: Strike) -> bool:
        """True เมื่อยังไม่เคยเขียน strike นี้ลงไฟล์ capture"""
        key = (strike.ts_iso, round(strike.lat, 4), round(strike.lon, 4))
        if key in self._captured_keys:
            return False
        self._captured_keys.add(key)
        if len(self._captured_keys) > 200_000:  # กันหน่วยความจำบวมตอนรันหลายวัน
            self._captured_keys.clear()
        return True

    def ingest_radar(self, frame) -> bool:
        """บันทึกผลวิเคราะห์ภาพเรดาร์หนึ่งภาพ และ capture ด้วยถ้าเปิดไว้"""
        stored = db.store_radar_frame(frame)
        if stored:
            self.radar_frames_stored += 1
            self.last_radar_frame_ts = frame.frame_ts
            if self.capture_path is not None:
                with self.capture_path.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(frame.to_record(), ensure_ascii=False) + "\n")
        return stored

    def _capture(self, strikes: list[Strike]) -> None:
        """เขียน strike จริงลง JSONL หนึ่งบรรทัดต่อหนึ่ง strike ไว้ replay ภายหลัง"""
        assert self.capture_path is not None
        with self.capture_path.open("a", encoding="utf-8") as handle:
            for strike in strikes:
                handle.write(json.dumps(strike.to_dict(), ensure_ascii=False) + "\n")

    def region_strikes(self, minutes: int) -> list[dict]:
        """ฟ้าผ่าในกรอบพื้นที่สนามไทยภายใน N นาทีล่าสุด (ใหม่สุดก่อน)"""
        cutoff = iso_minutes_ago(minutes)
        return sorted((s for s in list(self._region_recent) if s["ts_iso"] >= cutoff),
                      key=lambda s: s["ts_iso"], reverse=True)

    def status(self) -> dict:
        return {
            "received_total": self.received_total,
            "last_received_ts": self.last_received_ts,
            # strike ที่ตกในกรอบสี่เหลี่ยมรอบประเทศไทย (ก่อนกรองระยะจากสนาม)
            "in_region_total": self.in_region_total,
            "last_in_region_ts": self.last_in_region_ts,
            # strike ที่ใกล้สนามที่สุดตั้งแต่เริ่มรัน (แม้จะไกลเกินรัศมีที่เก็บ)
            "nearest_km_since_start": self.nearest_km,
            "nearest_stadium": self.nearest_stadium,
            "stored_total": self.stored_total,
            "last_strike_ts": self.last_strike_ts,
            "last_stored_at": self.last_stored_at,
            "radar_frames_stored": self.radar_frames_stored,
            "last_radar_frame_ts": self.last_radar_frame_ts,
            "capture_file": self.capture_path.name if self.capture_path else None,
        }
