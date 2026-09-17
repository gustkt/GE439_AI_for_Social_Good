"""ประกอบผลความเสี่ยงของสนาม - ใช้ร่วมกันทั้งโหมดสดและโหมด replay บนหน้าเว็บ

ทุกฟังก์ชันรับ `now` ได้: โหมดสดไม่ส่ง (= เวลาปัจจุบัน) ส่วนโหมด replay ส่ง "เวลาเสมือน"
ของพายุที่บันทึกไว้ ทำให้ทั้งสองโหมดใช้สูตรเดียวกันทุกบรรทัด ผลจึงเทียบกันได้ตรง ๆ
"""

from __future__ import annotations

from datetime import datetime, timedelta

from . import config
from .model.fusion import RISK_ORDER, fuse
from .model.nowcast import nowcast
from .model.tiering import evaluate_tier
from .timeutil import parse_iso

# ต้องดึงข้อมูลให้ครอบทั้งนาฬิกา all-clear (30 นาที) และหน้าต่าง nowcast (20 นาที)
DATA_WINDOW_MINUTES = max(config.ALL_CLEAR_MINUTES, config.NOWCAST_WINDOW_MINUTES)
# ผลเรดาร์ที่ต้องดึง: หน้าต่าง nowcast ของเรดาร์ + เผื่อภาพล่าช้า
RADAR_WINDOW_MINUTES = config.RADAR_NOWCAST_WINDOW_MINUTES + config.RADAR_STALE_MINUTES

_THAI = timedelta(hours=7)


def thai_clock(iso: str | None) -> str:
    moment = parse_iso(iso) if iso else None
    return (moment + _THAI).strftime("%H:%M") if moment else "?"


def radar_view(
    stadium: dict,
    history: list[dict],
    radar_ok: bool,
    now: datetime | None = None,
) -> tuple[dict, bool, str | None]:
    """ผลเรดาร์ของสนามหนึ่งแห่ง -> (ฟิลด์สำหรับ API, ขึ้น WATCH หรือไม่, เหตุผล)"""
    latest = history[0] if history else None
    nearest = latest["nearest_hotspot_km"] if latest else None
    watch = bool(radar_ok and nearest is not None and nearest <= config.RING_WATCH_KM)

    # nowcast ของเรดาร์: ติดตามจุดศูนย์ถ่วงของฝนแรงรอบสนามข้ามหลายภาพ แล้วประมาณว่า
    # จะแตะวง WATCH เมื่อไร (สูตรเดียวกับฟ้าผ่า แต่หน้าต่างยาวกว่าเพราะภาพห่างกัน 10 นาที)
    points = [
        {"ts_iso": e["frame_ts"], "lat": e["activity_lat"], "lon": e["activity_lon"]}
        for e in history
        if e["activity_pixels"]
    ]
    forecast = nowcast(
        points,
        stadium["lat"],
        stadium["lon"],
        target_radius_km=config.RING_WATCH_KM,
        window_minutes=config.RADAR_NOWCAST_WINDOW_MINUTES,
        bin_minutes=config.RADAR_NOWCAST_BIN_MINUTES,
        min_points=3,
        now=now,
    )
    reason = (
        f"เรดาร์: ฝน ≥ {config.RADAR_HOTSPOT_DBZ} dBZ ห่าง {nearest:.1f} กม. (ภาพ {thai_clock(latest['frame_ts'])} น.)"
        if watch
        else None
    )
    fields = {
        "radar_available": radar_ok,
        "radar_frame_ts": latest["frame_ts"] if latest else None,
        "radar_nearest_hotspot_km": nearest,
        "radar_max_dbz_watch": latest["max_dbz_watch"] if latest else None,
        "radar_eta_minutes": forecast.eta_minutes,
        # nowcast ใช้ข้อความของฟ้าผ่า ("strikes_ไม่พอ") - แปลให้ตรงกับบริบทของเรดาร์
        "radar_eta_reason": (
            "ไม่มีฝนแรงรอบสนามพอจะติดตาม" if forecast.reason in ("strikes_ไม่พอ", "ถังเวลาไม่พอ") else forecast.reason
        ),
        "radar_speed_kmph": forecast.speed_kmph,
    }
    return fields, watch, reason


def risk_for(
    stadium: dict,
    strikes: list[dict],
    monitored: bool,
    lightning: tuple[bool, str | None],
    radar: tuple[bool, str | None],
    radar_history: list[dict],
    now: datetime | None = None,
    lightning_sparse: bool = False,
) -> dict:
    """สถานะรวมของสนามหนึ่งแห่ง ณ เวลา now"""
    base = {
        "stadium_id": stadium["id"],
        "club": stadium["club"],
        "name": stadium["name"],
        "lat": stadium["lat"],
        "lon": stadium["lon"],
        "location_unverified": stadium["note"] == "verify",
        "monitored": monitored,
    }
    radar_fields, radar_watch, radar_reason = radar_view(stadium, radar_history, radar[0], now)

    if not monitored:
        # แหล่งข้อมูลฟ้าผ่าไม่ได้ติดตามสนามนี้ - ห้ามตอบ ALL_CLEAR เพราะ "ไม่มีข้อมูล"
        # ไม่ได้แปลว่า "ปลอดภัย" ผู้ใช้จะเข้าใจผิดได้อันตราย
        blank = {**evaluate_tier([], now).as_dict(), **nowcast([], stadium["lat"], stadium["lon"], now=now).as_dict()}
        return {
            **base,
            **blank,
            **radar_fields,
            "status": "UNMONITORED",
            "strike_count": None,
            "nowcast_reason": "ไม่ได้เฝ้าระวัง",
            "status_reasons": ["แหล่งข้อมูลฟ้าผ่าปัจจุบันไม่ได้ติดตามสนามนี้"],
        }

    tier = evaluate_tier(strikes, now).as_dict()
    fused = fuse(
        tier["status"],
        lightning[0],
        lightning[1],
        radar_watch=radar_watch,
        radar_reason=radar_reason,
        radar_available=radar[0],
        radar_unavailable_reason=radar[1],
        lightning_sparse=lightning_sparse,
    )
    return {
        **base,
        **tier,
        **nowcast(strikes, stadium["lat"], stadium["lon"], now=now).as_dict(),
        **radar_fields,
        # status = ผลรวมทุกหลักฐาน, lightning_status = ผลจากฟ้าผ่าอย่างเดียว (ไว้ตรวจย้อน)
        "status": fused.status,
        "lightning_status": tier["status"],
        "status_reasons": fused.reasons,
    }


def summarize(results: list[dict]) -> tuple[list[dict], dict]:
    """เรียงจากเสี่ยงมากไปน้อย และนับจำนวนแต่ละสถานะ"""
    ordered = sorted(results, key=lambda r: (RISK_ORDER.index(r["status"]), r["closest_km"] or 1e9))
    summary = {status: 0 for status in RISK_ORDER}
    for result in ordered:
        summary[result["status"]] += 1
    return ordered, summary
