"""ทดสอบชั้น model ด้วย assert ล้วน - ไม่ต้องมีเครือข่าย ไม่ต้องมีฐานข้อมูล

รัน:  python -m tests.test_model

ตั้งใจไม่ใช้ pytest เพื่อให้ไม่มี dependency เพิ่มจาก requirements.txt
"""

from __future__ import annotations

from datetime import timedelta

from app import config
from app.geo import haversine_km, offset_km
from app.model.nowcast import nowcast
from app.model.tiering import evaluate_tier
from app.timeutil import to_iso, utcnow

STADIUM_LAT, STADIUM_LON = 14.0, 100.0
APPROACH_BEARING = 225.0

_passed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} ล้มเหลว {detail}")
    _passed.append(name)


def strike_at(distance_km: float, minutes_ago: float) -> dict:
    """สร้าง strike สังเคราะห์ที่ระยะและเวลาที่กำหนด (วางตามแนวเข้าหาสนาม)"""
    lat, lon = offset_km(
        STADIUM_LAT, STADIUM_LON, distance_km, APPROACH_BEARING
    )
    return {
        "ts_iso": to_iso(utcnow() - timedelta(minutes=minutes_ago)),
        "lat": lat,
        "lon": lon,
        "distance_km": distance_km,
        "source": "test",
    }


# ---------------------------------------------------------------- geo


def test_geo() -> None:
    # ไป-กลับต้องได้ระยะเดิม ถ้าสองฟังก์ชันนี้เพี้ยน ทุกอย่างที่เหลือเพี้ยนตาม
    lat, lon = offset_km(STADIUM_LAT, STADIUM_LON, 13.0, 225.0)
    back = haversine_km(STADIUM_LAT, STADIUM_LON, lat, lon)
    check("geo: offset -> haversine กลับมาได้ระยะเดิม", abs(back - 13.0) < 0.05, f"ได้ {back:.3f}")


# ------------------------------------------------------------- tiering


def test_tiering_rings() -> None:
    check("tiering: ไม่มี strike = ALL_CLEAR", evaluate_tier([]).status == "ALL_CLEAR")

    check(
        "tiering: 9 กม. = DANGER",
        evaluate_tier([strike_at(9.0, 1)]).status == "DANGER",
    )
    check(
        "tiering: 12 กม. = SUSPEND",
        evaluate_tier([strike_at(12.0, 1)]).status == "SUSPEND",
    )
    check(
        "tiering: 15 กม. = WATCH",
        evaluate_tier([strike_at(15.0, 1)]).status == "WATCH",
    )
    check(
        "tiering: 25 กม. = ALL_CLEAR (ไกลกว่าวงเฝ้าระวัง)",
        evaluate_tier([strike_at(25.0, 1)]).status == "ALL_CLEAR",
    )

    # สถานะต้องยึดวงชั้นในสุด ไม่ใช่ strike ล่าสุด
    mixed = evaluate_tier([strike_at(9.0, 20), strike_at(15.0, 1)])
    check("tiering: ยึด strike ที่ใกล้ที่สุด ไม่ใช่ล่าสุด", mixed.status == "DANGER")


def test_tiering_window() -> None:
    old = evaluate_tier([strike_at(9.0, config.ALL_CLEAR_MINUTES + 5)])
    check("tiering: strike เก่ากว่าหน้าต่าง 30 นาที ถูกมองข้าม", old.status == "ALL_CLEAR")


def test_all_clear_clock() -> None:
    # strike ที่ 12 กม. เมื่อ 25 นาทีก่อน -> เหลืออีก ~5 นาทีจึงจะ all-clear
    result = evaluate_tier([strike_at(12.0, 25)])
    remaining = (result.all_clear_seconds_remaining or 0) / 60.0
    check(
        "all-clear: นับถอยหลังถูกต้องจาก strike ในวงสั่งหยุด",
        4.0 < remaining < 6.0,
        f"เหลือ {remaining:.1f} นาที",
    )

    # strike ใหม่ที่ 15 กม. อยู่ *นอก* วงสั่งหยุด จึงต้องไม่รีเซ็ตนาฬิกา
    with_outer = evaluate_tier([strike_at(12.0, 25), strike_at(15.0, 0)])
    remaining_2 = (with_outer.all_clear_seconds_remaining or 0) / 60.0
    check(
        "all-clear: strike นอกวง 13 กม. ไม่รีเซ็ตนาฬิกา",
        abs(remaining_2 - remaining) < 1.0,
        f"{remaining:.1f} -> {remaining_2:.1f} นาที",
    )

    # แต่ strike ใหม่ *ใน* วงสั่งหยุด ต้องรีเซ็ตเป็น 30 นาทีเต็ม
    reset = evaluate_tier([strike_at(12.0, 25), strike_at(12.0, 0)])
    remaining_3 = (reset.all_clear_seconds_remaining or 0) / 60.0
    check(
        "all-clear: strike ในวง 13 กม. รีเซ็ตนาฬิกาเป็น 30 นาที",
        remaining_3 > config.ALL_CLEAR_MINUTES - 1.5,
        f"เหลือ {remaining_3:.1f} นาที",
    )

    check(
        "all-clear: ไม่มี strike ในวงสั่งหยุด = ไม่มีนาฬิกา",
        evaluate_tier([strike_at(15.0, 1)]).all_clear_seconds_remaining is None,
    )


# ------------------------------------------------------------- nowcast


def _track(start_km: float, speed_kmpm: float, approaching: bool) -> list[dict]:
    """สร้างรอย strike ของพายุที่เคลื่อนที่เป็นเส้นตรงตลอด 20 นาทีล่าสุด"""
    strikes = []
    for age in range(0, config.NOWCAST_WINDOW_MINUTES):
        travelled = speed_kmpm * age
        distance = start_km + travelled if approaching else start_km - travelled
        for _ in range(3):  # หลาย strike ต่อนาที เหมือนข้อมูลจริง
            strikes.append(strike_at(max(distance, 0.5), age))
    return strikes


def test_nowcast_approaching() -> None:
    # พายุอยู่ห่าง 25 กม. วิ่งเข้าหาด้วย 0.8 กม./นาที
    # -> ต้องใช้ (25 - 13) / 0.8 = 15 นาที จึงแตะวงสั่งหยุด
    result = nowcast(_track(25.0, 0.8, approaching=True), STADIUM_LAT, STADIUM_LON)
    check(
        "nowcast: พายุเข้าใกล้ -> มี ETA",
        result.eta_minutes is not None,
        f"reason={result.reason}",
    )
    check(
        "nowcast: ETA ใกล้เคียงค่าที่คำนวณมือ (15 นาที)",
        abs((result.eta_minutes or 0) - 15.0) < 2.0,
        f"ได้ {result.eta_minutes} นาที",
    )
    check(
        "nowcast: ความเร็วใกล้เคียง 48 กม./ชม.",
        abs((result.speed_kmph or 0) - 48.0) < 6.0,
        f"ได้ {result.speed_kmph} กม./ชม.",
    )


def test_nowcast_receding() -> None:
    # start_km=20 แบบ "ไม่เข้าใกล้" = เมื่อ 19 นาทีก่อนอยู่ใกล้กว่านี้ แล้วถอยออก
    result = nowcast(_track(20.0, 0.8, approaching=False), STADIUM_LAT, STADIUM_LON)
    check(
        "nowcast: พายุถอยห่าง -> eta เป็น null",
        result.eta_minutes is None,
        f"ได้ {result.eta_minutes} (reason={result.reason})",
    )
    check(
        "nowcast: บอกเหตุผลว่ากำลังถอยห่าง",
        result.reason == "กำลังถอยห่าง",
        f"ได้ '{result.reason}'",
    )


def test_nowcast_insufficient_data() -> None:
    check(
        "nowcast: ไม่มี strike -> null",
        nowcast([], STADIUM_LAT, STADIUM_LON).eta_minutes is None,
    )

    # strike เยอะแต่กระจุกอยู่ในถังเวลาเดียว = บอกทิศทางไม่ได้
    single_bin = [strike_at(20.0, 0.5) for _ in range(10)]
    result = nowcast(single_bin, STADIUM_LAT, STADIUM_LON)
    check(
        "nowcast: ข้อมูลอยู่ในถังเวลาเดียว -> null",
        result.eta_minutes is None,
        f"ได้ {result.eta_minutes} (reason={result.reason})",
    )


def test_nowcast_already_inside() -> None:
    result = nowcast(_track(5.0, 0.5, approaching=True), STADIUM_LAT, STADIUM_LON)
    check(
        "nowcast: พายุอยู่ในวงแล้ว -> ETA = 0",
        result.eta_minutes == 0.0,
        f"ได้ {result.eta_minutes} (reason={result.reason})",
    )


def test_nowcast_implausible_speed() -> None:
    # 3 กม./นาที = 180 กม./ชม. เร็วเกินพายุฝนฟ้าคะนองจริง -> ต้องไม่รายงานตัวเลข
    result = nowcast(_track(25.0, 3.0, approaching=True), STADIUM_LAT, STADIUM_LON)
    check(
        "nowcast: ความเร็วเกินจริง -> ไม่รายงาน ETA และความเร็ว",
        result.eta_minutes is None and result.speed_kmph is None and "เกินจริง" in result.reason,
        f"ได้ eta={result.eta_minutes} speed={result.speed_kmph} reason={result.reason}",
    )


def main() -> None:
    tests = [
        test_geo,
        test_tiering_rings,
        test_tiering_window,
        test_all_clear_clock,
        test_nowcast_approaching,
        test_nowcast_receding,
        test_nowcast_insufficient_data,
        test_nowcast_already_inside,
        test_nowcast_implausible_speed,
    ]
    for test in tests:
        test()
    for name in _passed:
        print(f"  ok  {name}")
    print(f"\nผ่านทั้งหมด {len(_passed)} ข้อ")


if __name__ == "__main__":
    main()
