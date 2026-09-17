"""กฎความปลอดภัย: แปลงระยะของ strike เป็นสถานะสั่งการ

อิงแนวปฏิบัติ NCAA Lightning Safety ซึ่งเป็นมาตรฐานที่กีฬากลางแจ้งใช้กันแพร่หลาย
    * 8 ไมล์ (~13 กม.) = ระยะที่ต้อง "หยุดการแข่งขันและอพยพ"
      มาจากข้อเท็จจริงว่าฟ้าผ่าสามารถกระโดดออกจากเมฆฝนได้ไกลระดับ 10 กม.
      ("bolt from the blue") ดังนั้นการรอให้ฝนตกก่อนจึงสายเกินไป
    * ต้องเว้น 30 นาที นับจาก strike ครั้งสุดท้ายในวงนั้น จึงจะกลับมาเล่นได้
      และถ้ามี strike ใหม่เข้ามาในวง ให้เริ่มนับ 30 นาทีใหม่ทั้งหมด

MVP นี้เพิ่มวงชั้นนอก 16 กม. เป็น WATCH เพื่อให้ทีมงานสนามมีเวลาเตรียมตัว
ก่อนถึงจุดที่ต้องสั่งหยุดจริง และวงชั้นใน 10 กม. เป็น DANGER
(ทุกคนต้องอยู่ในที่กำบังแล้ว) รัศมีทั้งสามค่าปรับได้จาก .env
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta

from .. import config
from ..timeutil import iso_minutes_ago, parse_iso, to_iso, utcnow

# เรียงจากรุนแรงที่สุดไปเบาที่สุด - ใช้ทั้งตอนตัดสินและตอนส่งให้ frontend
STATUS_ORDER = ["DANGER", "SUSPEND", "WATCH", "ALL_CLEAR"]


@dataclass
class TierResult:
    status: str
    closest_km: float | None
    closest_ts: str | None
    last_strike_ts: str | None
    strike_count: int
    all_clear_at: str | None
    all_clear_seconds_remaining: int | None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "closest_km": self.closest_km,
            "closest_ts": self.closest_ts,
            "last_strike_ts": self.last_strike_ts,
            "strike_count": self.strike_count,
            "all_clear_at": self.all_clear_at,
            "all_clear_seconds_remaining": self.all_clear_seconds_remaining,
        }


def evaluate_tier(strikes: list[dict], now=None) -> TierResult:
    """ตัดสินสถานะของสนามหนึ่งแห่งจาก strike ที่เกิดใน 30 นาทีล่าสุด

    `strikes` คือ list ของ dict ที่มีอย่างน้อย `ts_iso` และ `distance_km`
    (ระยะจาก *สนามนั้น* ถึง strike) - ผู้เรียกอาจส่งข้อมูลที่กว้างกว่า
    หน้าต่างเวลามาได้ ฟังก์ชันนี้กรองให้เอง

    `now` = เวลาที่ใช้ตัดสิน (default = เวลาปัจจุบัน) โหมด replay บนหน้าเว็บส่ง
    "เวลาเสมือน" มา เพื่อคำนวณสถานะ ณ ช่วงเวลาใดของพายุที่บันทึกไว้ก็ได้
    """
    window_start = iso_minutes_ago(config.ALL_CLEAR_MINUTES, now)
    recent = [
        s
        for s in strikes
        if s.get("ts_iso")
        and s["ts_iso"] >= window_start
        and s.get("distance_km") is not None
    ]

    if not recent:
        return TierResult(
            status="ALL_CLEAR",
            closest_km=None,
            closest_ts=None,
            last_strike_ts=None,
            strike_count=0,
            all_clear_at=None,
            all_clear_seconds_remaining=None,
        )

    closest = min(recent, key=lambda s: s["distance_km"])
    latest = max(recent, key=lambda s: s["ts_iso"])
    closest_km = float(closest["distance_km"])

    # สถานะ = วงชั้นในสุดที่ยังมี strike อยู่ในหน้าต่าง 30 นาที
    if closest_km <= config.RING_DANGER_KM:
        status = "DANGER"
    elif closest_km <= config.RING_SUSPEND_KM:
        status = "SUSPEND"
    elif closest_km <= config.RING_WATCH_KM:
        status = "WATCH"
    else:
        # มี strike อยู่ แต่ไกลกว่าวงเฝ้าระวัง = ยังถือว่าปลอดภัย
        status = "ALL_CLEAR"

    # นาฬิกา all-clear นับจาก strike ล่าสุดที่เข้ามาใน "วงสั่งหยุด" เท่านั้น
    # (strike ที่ 15 กม. ทำให้ขึ้น WATCH ได้ แต่ไม่รีเซ็ตนาฬิกา 30 นาที)
    in_suspend_ring = [
        s for s in recent if float(s["distance_km"]) <= config.RING_SUSPEND_KM
    ]
    all_clear_at = None
    seconds_remaining = None
    if in_suspend_ring:
        trigger = max(in_suspend_ring, key=lambda s: s["ts_iso"])
        trigger_dt = parse_iso(trigger["ts_iso"])
        if trigger_dt is not None:
            deadline = trigger_dt + timedelta(minutes=config.ALL_CLEAR_MINUTES)
            all_clear_at = to_iso(deadline)
            seconds_remaining = max(
                0, int((deadline - (now or utcnow())).total_seconds())
            )

    return TierResult(
        status=status,
        closest_km=round(closest_km, 2),
        closest_ts=closest["ts_iso"],
        last_strike_ts=latest["ts_iso"],
        strike_count=len(recent),
        all_clear_at=all_clear_at,
        all_clear_seconds_remaining=seconds_remaining,
    )


def rings() -> list[dict]:
    """รัศมีทั้งสามวง ส่งให้ frontend วาดเป็นวงกลมบนแผนที่"""
    return [
        {"status": "DANGER", "radius_km": config.RING_DANGER_KM},
        {"status": "SUSPEND", "radius_km": config.RING_SUSPEND_KM},
        {"status": "WATCH", "radius_km": config.RING_WATCH_KM},
    ]
