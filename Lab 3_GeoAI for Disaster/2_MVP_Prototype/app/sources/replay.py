"""ReplaySource - เล่นข้อมูลจริงที่ capture ไว้ ย้อนหลังตามเวลา (ฟ้าผ่า + เรดาร์)

ใช้ตอน demo วันที่ฟ้าใส โดยไม่ต้องใช้ข้อมูลปลอม: ทุก record ในไฟล์คือสิ่งที่
เกิดขึ้นจริงและถูกบันทึกไว้ตอนที่เกิด แล้วเลื่อนเวลามาเล่นใหม่เท่านั้น

ไฟล์ capture มี record 2 ชนิด (บรรทัดละหนึ่ง record):
  * strike : ฟ้าผ่าหนึ่งครั้ง (ไฟล์รุ่นแรกไม่มีฟิลด์ kind = strike ทั้งหมด)
  * radar  : ผลวิเคราะห์ภาพเรดาร์หนึ่งภาพ (kind = "radar") - cell และระยะถึงสนาม
ทั้งสองชนิดถูกเลื่อนเวลาด้วยค่าคงที่เดียวกัน ความสัมพันธ์ทางเวลาระหว่างฟ้าผ่ากับ
เรดาร์จึงเหมือนตอนที่เกิดจริงทุกประการ

ระบบนี้ไม่มีโหมดข้อมูลจำลอง - แหล่งข้อมูลมีแค่ blitzortung, xweather และ replay
(ข้อมูลสังเคราะห์มีเฉพาะใน tests/ สำหรับ unit test)
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import config
from ..timeutil import parse_iso, to_iso, utcnow
from .base import LightningSource, OnStrikes, Strike

log = logging.getLogger(__name__)

_THAI_TZ = timezone(timedelta(hours=7))


def record_time(record: dict) -> str | None:
    """เวลาของ record ไม่ว่าจะเป็นฟ้าผ่าหรือภาพเรดาร์"""
    return record.get("frame_ts") if record.get("kind") == "radar" else record.get("ts_iso")


def resolve_capture_path(name: str) -> Path:
    """ชื่อไฟล์ใน data/captures หรือ path เต็ม -> Path ที่มีอยู่จริง"""
    if not name:
        raise RuntimeError(
            "LIGHTNING_SOURCE=replay ต้องระบุ REPLAY_FILE ใน .env "
            "(ไฟล์ .jsonl ใน data/captures ที่ได้จาก CAPTURE=true)"
        )
    candidate = Path(name)
    for path in (candidate, config.CAPTURE_DIR / name):
        if path.is_file():
            return path
    raise RuntimeError(f"ไม่พบไฟล์ replay: {name} (ค้นใน data/captures ด้วยแล้ว)")


def load_capture(path: Path) -> list[dict]:
    """อ่าน JSONL ข้ามบรรทัดเสีย ตัดตัวซ้ำ เรียงตามเวลาเกิดจริง

    ตัดซ้ำเผื่อไฟล์เก่าที่บันทึกก่อนมีการกรองซ้ำใน ingest และเผื่อการรวมหลายไฟล์
    """
    records = []
    seen: set[tuple] = set()
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                record = json.loads(line)
                moment = record_time(record)
                if not moment or parse_iso(moment) is None:
                    continue
                if record.get("kind") == "radar":
                    key: tuple = ("radar", moment)
                else:
                    key = ("strike", moment, round(float(record["lat"]), 4), round(float(record["lon"]), 4))
                if key in seen:
                    continue
                seen.add(key)
                records.append(record)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError):
                continue
    records.sort(key=record_time)
    return records


class ReplaySource(LightningSource):
    """เล่น record จริงจากไฟล์ capture โดยเลื่อนเวลาให้ record แรก = ตอนเริ่มเล่น

    หมายเหตุ: REPLAY_SPEED > 1 บีบเวลาให้สั้นลง ความเร็วพายุที่ nowcast ประมาณได้
    และนาฬิกา all-clear จะไม่ตรงกับความจริงตามสัดส่วนนั้น
    """

    name = "replay"
    TICK_SECONDS = 1.0

    def __init__(self, file_name: str | None = None, speed: float | None = None) -> None:
        self.path = resolve_capture_path(file_name if file_name is not None else config.REPLAY_FILE)
        self.speed = max(0.1, speed if speed is not None else config.REPLAY_SPEED)
        self.records = load_capture(self.path)
        if not self.records:
            raise RuntimeError(f"ไฟล์ replay {self.path.name} ไม่มี record ที่อ่านได้")

        first = parse_iso(record_time(self.records[0]))
        last = parse_iso(record_time(self.records[-1]))
        assert first is not None and last is not None
        self.original_start = first
        self.original_end = last
        self.strikes_total = sum(1 for r in self.records if r.get("kind") != "radar")
        self.radar_frames_total = len(self.records) - self.strikes_total
        self.original_sources = sorted(
            {r.get("source", "?") for r in self.records if r.get("kind") != "radar"}
        )
        local = first.astimezone(_THAI_TZ)
        speed_note = "" if self.speed == 1 else f" x{self.speed:g}"
        self.label = f"REPLAY ข้อมูลจริง {local:%Y-%m-%d %H:%M} (เวลาไทย){speed_note}"

        # main.py ตั้งค่านี้ให้ เพื่อส่งภาพเรดาร์ที่เล่นซ้ำไปเก็บลง DB
        self.radar_sink: Callable | None = None
        self.last_radar_emitted: datetime | None = None
        self.emitted = 0
        self.finished = False

    async def run(self, on_strikes: OnStrikes) -> None:
        from .rainviewer import RadarFrame  # import ตอนใช้ เลี่ยงโหลด Pillow โดยไม่จำเป็น

        log.info(
            "เริ่ม replay %s: strike %s จุด ภาพเรดาร์ %s ภาพ ช่วง %s ถึง %s",
            self.path.name, self.strikes_total, self.radar_frames_total,
            to_iso(self.original_start), to_iso(self.original_end),
        )
        started = utcnow()
        index = 0
        while index < len(self.records):
            elapsed_original = (utcnow() - started) * self.speed
            strikes: list[Strike] = []
            frames = []
            while index < len(self.records):
                record = self.records[index]
                original = parse_iso(record_time(record))
                assert original is not None
                offset = original - self.original_start
                if offset > elapsed_original:
                    break
                shifted = to_iso(started + offset / self.speed)
                if record.get("kind") == "radar":
                    frames.append(RadarFrame.from_record(record, frame_ts=shifted, source=f"replay:{self.path.name}"))
                else:
                    strikes.append(
                        Strike(
                            ts_iso=shifted,
                            lat=float(record["lat"]),
                            lon=float(record["lon"]),
                            source=f"replay:{self.path.name}",
                            polarity=record.get("polarity"),
                            station_count=record.get("station_count"),
                        )
                    )
                index += 1

            if strikes:
                self.emitted += len(strikes)
                await asyncio.to_thread(on_strikes, strikes)
            for frame in frames:
                if self.radar_sink is not None:
                    await asyncio.to_thread(self.radar_sink, frame)
                self.last_radar_emitted = utcnow()
            await asyncio.sleep(self.TICK_SECONDS)

        self.finished = True
        log.info("replay %s จบแล้ว", self.path.name)

    def radar_availability(self) -> tuple[bool, str | None]:
        """ภาพเรดาร์จากไฟล์ replay ยังสดพอจะใช้ตัดสินหรือไม่ (เทียบกับเวลาที่เล่นซ้ำ)"""
        if self.radar_frames_total == 0:
            return False, "ไฟล์ replay นี้ไม่มีข้อมูลเรดาร์ (บันทึกก่อนเพิ่มโมดูลเรดาร์)"
        if self.last_radar_emitted is None:
            return False, "กำลังรอภาพเรดาร์ชุดแรกในไฟล์ replay"
        age = (utcnow() - self.last_radar_emitted).total_seconds() / 60 * self.speed
        if age > config.RADAR_STALE_MINUTES:
            return False, f"ภาพเรดาร์ในไฟล์ replay ขาดช่วง {age:.0f} นาที (ตามเวลาต้นฉบับ)"
        return True, None

    def status(self) -> dict:
        return {
            "file": self.path.name,
            "original_start": to_iso(self.original_start),
            "original_end": to_iso(self.original_end),
            "original_sources": self.original_sources,
            "speed": self.speed,
            "strikes_total": self.strikes_total,
            "radar_frames_total": self.radar_frames_total,
            "emitted": self.emitted,
            "finished": self.finished,
        }
