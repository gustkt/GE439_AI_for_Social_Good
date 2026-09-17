"""ตัวสร้างรอยพายุสังเคราะห์ สำหรับ unit test เท่านั้น

⚠ นี่ไม่ใช่แหล่งข้อมูลของระบบ และห้ามนำผลจากไฟล์นี้ไปแสดงเป็นผลลัพธ์
ระบบจริงรับข้อมูลจาก Blitzortung / Xweather / replay ของข้อมูลจริงเท่านั้น

มีไว้เพื่อตอบคำถามเดียว: ถ้าพายุเคลื่อนเข้าหาสนามด้วยความเร็วที่รู้ค่าแน่นอน
สูตร tiering กับ nowcast จะคำนวณกลับมาได้ตรงหรือไม่ - เป็นการทดสอบความถูกต้อง
ของสูตร ไม่ใช่การจำลองข้อมูลเพื่อ demo
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta

from app.geo import offset_km
from app.sources.base import Strike
from app.timeutil import to_iso

# ค่าที่ใช้ในเทสต์: พายุเริ่มห่าง 28 กม. วิ่งเข้าหาสนามด้วย 0.8 กม./นาที (48 กม./ชม.)
START_DISTANCE_KM = 28.0
SPEED_KMPM = 0.8
BEARING_DEG = 225.0
CELL_RADIUS_KM = 6.0
STRIKES_PER_MIN = 12.0


def generate_storm_track(
    lat: float,
    lon: float,
    t0: datetime,
    start_minutes: float,
    end_minutes: float,
    seed: int = 7,
) -> list[Strike]:
    """strike สังเคราะห์ในช่วงนาทีที่ [start_minutes, end_minutes] นับจาก t0

    ศูนย์กลางพายุอยู่ห่างสนาม START_DISTANCE_KM - SPEED_KMPM x นาที
    และ strike กระจายสุ่มในวงกลมรัศมี CELL_RADIUS_KM รอบศูนย์กลางนั้น
    """
    rng = random.Random(seed)
    span = end_minutes - start_minutes
    count = _poisson(rng, STRIKES_PER_MIN * max(span, 0.0))

    strikes = []
    for _ in range(count):
        minute = start_minutes + span * rng.random()
        centre_distance = START_DISTANCE_KM - SPEED_KMPM * minute
        centre = offset_km(lat, lon, centre_distance, BEARING_DEG)
        spread = CELL_RADIUS_KM * math.sqrt(rng.random())
        s_lat, s_lon = offset_km(centre[0], centre[1], spread, rng.uniform(0, 360))
        strikes.append(
            Strike(
                ts_iso=to_iso(t0 + timedelta(minutes=minute)),
                lat=round(s_lat, 6),
                lon=round(s_lon, 6),
                source="test-fixture",
            )
        )
    return sorted(strikes, key=lambda s: s.ts_iso)


def _poisson(rng: random.Random, mean: float) -> int:
    """สุ่มจำนวนเหตุการณ์แบบ Poisson (Knuth) - ฟ้าผ่าเกิดเป็นกระจุก ไม่สม่ำเสมอ"""
    if mean <= 0:
        return 0
    limit, k, product = math.exp(-mean), 0, rng.random()
    while product > limit:
        k += 1
        product *= rng.random()
    return k
