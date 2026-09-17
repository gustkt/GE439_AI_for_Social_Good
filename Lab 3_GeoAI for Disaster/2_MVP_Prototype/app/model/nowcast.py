"""Nowcast: ประมาณว่าพายุจะมาถึงสนามในอีกกี่นาที

นี่คือส่วนที่ทำให้ระบบเป็น "prediction" ไม่ใช่แค่ "detection" - ระบบตรวจจับ
บอกได้แค่ว่า *ตอนนี้* อันตรายหรือยัง แต่ผู้จัดการแข่งขันต้องการเวลาล่วงหน้า
เพื่อประกาศและอพยพคนออกจากอัฒจันทร์ ซึ่งกินเวลาหลายนาที

วิธีที่ใช้ (ตั้งใจให้เรียบง่ายและอธิบายได้ ไม่ใช่ deep learning):
  1. เอา strike ในหน้าต่าง NOWCAST_WINDOW_MINUTES นาทีล่าสุด (default 20)
  2. แบ่งเป็นถังเวลาละ NOWCAST_BIN_MINUTES นาที (default 5 -> ได้ 4 ถัง)
     โดย bin ตาม *เวลาที่ฟ้าผ่าเกิดจริง* (ts_iso) ไม่ใช่เวลาที่เรารับข้อมูล
     เพราะ stream อาจส่งมาช้า และ polling ได้ข้อมูลมาเป็นก้อนทีละหลายนาที
     แล้วหา centroid (จุดศูนย์ถ่วง) ของ strike ในแต่ละถัง
     -> เท่ากับลดสัญญาณรบกวนจากการกระจายตัวแบบสุ่มของฟ้าผ่าแต่ละครั้ง
  3. fit เส้นตรง x(t), y(t) จาก centroid เหล่านั้นด้วย least squares
     บนระนาบท้องถิ่นที่มีสนามเป็นจุดกำเนิด -> ได้เวกเตอร์ความเร็ว (vx, vy)
  4. แก้สมการกำลังสอง |p(t)| = รัศมีวงสั่งหยุด เพื่อหาเวลาที่ขอบพายุ
     จะแตะวง SUSPEND -> นั่นคือ ETA

ข้อจำกัดที่ต้องบอกในรายงาน: สมมติว่าพายุเคลื่อนที่เป็นเส้นตรงด้วยความเร็วคงที่
และไม่ได้จำลองการก่อตัว/สลายตัวของ cell จึงเชื่อถือได้เฉพาะช่วงสั้น ๆ
(ไม่กี่สิบนาที) ซึ่งก็ตรงกับนิยามของคำว่า nowcasting
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .. import config
from ..geo import to_local_xy
from ..timeutil import iso_minutes_ago, minutes_since


@dataclass
class NowcastResult:
    """ผลของ nowcast - `eta_minutes` เป็น None ได้เสมอ และ None มีความหมาย"""

    eta_minutes: float | None
    speed_kmph: float | None
    bearing_deg: float | None
    centroid_distance_km: float | None
    bins_used: int
    reason: str

    def as_dict(self) -> dict:
        return {
            "nowcast_eta_minutes": self.eta_minutes,
            "nowcast_speed_kmph": self.speed_kmph,
            "nowcast_bearing_deg": self.bearing_deg,
            "nowcast_centroid_distance_km": self.centroid_distance_km,
            "nowcast_bins_used": self.bins_used,
            "nowcast_reason": self.reason,
        }


def _empty(reason: str, bins: int = 0) -> NowcastResult:
    return NowcastResult(None, None, None, None, bins, reason)


def nowcast(
    strikes: list[dict],
    stadium_lat: float,
    stadium_lon: float,
    target_radius_km: float | None = None,
    window_minutes: float | None = None,
    bin_minutes: float | None = None,
    min_points: int = 4,
    now=None,
) -> NowcastResult:
    """ประมาณ ETA ที่พายุจะแตะวงสั่งหยุดของสนามนี้

    `strikes` ต้องมี `ts_iso`, `lat`, `lon` - ผู้เรียกควรส่ง strike ในรัศมี
    ที่กว้างกว่าวง WATCH (INGEST_RADIUS_KM default 30 กม.) มาด้วย ไม่งั้นจะ
    "เห็น" พายุก็ต่อเมื่อมันเข้ามาใกล้เกินกว่าจะเตือนล่วงหน้าได้แล้ว
    """
    radius = (
        target_radius_km if target_radius_km is not None else config.RING_SUSPEND_KM
    )
    # ค่า default สำหรับฟ้าผ่า เรดาร์ส่งค่าของตัวเองมา (ภาพห่างกัน 10 นาที)
    window_start = iso_minutes_ago(window_minutes or config.NOWCAST_WINDOW_MINUTES, now)
    recent = [
        s
        for s in strikes
        if s.get("ts_iso")
        and s["ts_iso"] >= window_start
        and s.get("lat") is not None
        and s.get("lon") is not None
    ]
    if len(recent) < min_points:
        return _empty("strikes_ไม่พอ")

    # --- 1) แบ่งถังเวลา แล้วหา centroid ของแต่ละถัง ------------------------
    bin_minutes = max(1, bin_minutes or config.NOWCAST_BIN_MINUTES)
    buckets: dict[int, list[tuple[float, float, float]]] = {}
    for s in recent:
        age = minutes_since(s["ts_iso"], now)
        if age is None or age < 0:
            age = 0.0
        buckets.setdefault(int(age // bin_minutes), []).append(
            (-age, float(s["lat"]), float(s["lon"]))
        )

    if len(buckets) < 2:
        # มี strike อยู่ในถังเวลาเดียว = ยังบอกทิศทางการเคลื่อนที่ไม่ได้
        return _empty("ถังเวลาไม่พอ", len(buckets))

    samples: list[tuple[float, float, float]] = []  # (t นาที, x กม., y กม.)
    for members in buckets.values():
        n = len(members)
        t = sum(m[0] for m in members) / n
        lat = sum(m[1] for m in members) / n
        lon = sum(m[2] for m in members) / n
        x, y = to_local_xy(lat, lon, stadium_lat, stadium_lon)
        samples.append((t, x, y))
    samples.sort()

    # --- 2) fit เส้นตรงแยกแกน x และ y (t = 0 คือ "เดี๋ยวนี้") --------------
    ts = [s[0] for s in samples]
    fit_x = _least_squares(ts, [s[1] for s in samples])
    fit_y = _least_squares(ts, [s[2] for s in samples])
    if fit_x is None or fit_y is None:
        return _empty("fit_ไม่ได้", len(samples))

    vx, x0 = fit_x
    vy, y0 = fit_y

    speed_kmpm = math.hypot(vx, vy)
    current_distance = math.hypot(x0, y0)
    bearing = (math.degrees(math.atan2(vx, vy)) + 360.0) % 360.0

    base = NowcastResult(
        eta_minutes=None,
        speed_kmph=round(speed_kmpm * 60.0, 1),
        bearing_deg=round(bearing, 1),
        centroid_distance_km=round(current_distance, 2),
        bins_used=len(samples),
        reason="",
    )

    if speed_kmpm * 60.0 > config.NOWCAST_MAX_SPEED_KMPH:
        # พายุฝนฟ้าคะนองจริงแทบไม่เคยเร็วเกิน ~100 กม./ชม. ค่าที่สูงกว่านี้มักมาจากถังเวลาน้อย
        # และ centroid กระโดดข้าม cell (พบจริงในพายุ 16 ก.ย. 2026: 349 กม./ชม. จาก 2 ถัง)
        # จึงไม่รายงานทั้งความเร็วและ ETA ดีกว่าให้ตัวเลขที่ผิดชัดเจน
        return _empty("ความเร็วเกินจริง (ข้อมูลน้อย)", len(samples))

    if speed_kmpm < config.NOWCAST_MIN_SPEED_KMPM:
        # พายุอยู่กับที่ / สัญญาณเป็นสัญญาณรบกวนล้วน -> ไม่เดาดีกว่าเดาผิด
        base.reason = "เคลื่อนที่ไม่ชัดเจน"
        return base

    # --- 3) หาเวลาที่ |p(t)| = radius  (สมการกำลังสองใน t) -----------------
    a = vx * vx + vy * vy
    b = 2.0 * (x0 * vx + y0 * vy)
    c = x0 * x0 + y0 * y0 - radius * radius

    if c <= 0:
        # centroid อยู่ในวงสั่งหยุดแล้ว - เตือนล่วงหน้าไม่ทันแล้ว ให้ตอบ 0
        base.eta_minutes = 0.0
        base.reason = "อยู่ในวงแล้ว"
        return base

    discriminant = b * b - 4 * a * c
    if discriminant <= 0:
        # วิถีเฉียดไปข้าง ๆ ไม่ตัดวงสั่งหยุด
        base.reason = "ไม่ผ่านวงสั่งหยุด"
        return base

    root = (-b - math.sqrt(discriminant)) / (2 * a)
    if root < 0:
        # จุดตัดอยู่ในอดีต = พายุกำลังถอยห่างออกไป
        base.reason = "กำลังถอยห่าง"
        return base
    if root > config.NOWCAST_MAX_ETA_MINUTES:
        base.reason = "ไกลเกินขอบเขตพยากรณ์"
        return base

    base.eta_minutes = round(root, 1)
    base.reason = "กำลังเข้าใกล้"
    return base


def _least_squares(
    xs: list[float], ys: list[float]
) -> tuple[float, float] | None:
    """คืน (slope, intercept) ของเส้นตรงที่ fit ดีที่สุด - None ถ้า x ซ้ำกันหมด"""
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    denominator = sum((x - mean_x) ** 2 for x in xs)
    if denominator == 0:
        return None
    slope = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys)) / denominator
    return slope, mean_y - slope * mean_x
