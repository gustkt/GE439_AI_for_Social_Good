"""โหมด replay บนหน้าเว็บ: ดูพายุจริงที่บันทึกไว้ ณ "เวลาเสมือน" ใดก็ได้

ต่างจาก LIGHTNING_SOURCE=replay (ป้อนข้อมูลเข้าระบบตามเวลาจริง ต้องรีสตาร์ท):
  * ไม่แตะฐานข้อมูลและไม่หยุดระบบสด - เซิร์ฟเวอร์ยังเก็บข้อมูลสดต่อไประหว่างดู replay
  * คำนวณสถานะ ณ เวลา t จากไฟล์ capture โดยตรง ด้วยสูตรชุดเดียวกับโหมดสด
    (app/risk.py -> tiering / nowcast / fusion) แค่ส่ง t เป็น now
  * ผู้ใช้เลื่อน/เล่น/เร่งเวลาได้อิสระ แต่ความเร็วพายุและนาฬิกา all-clear ยังตรงความจริง
    เพราะทุกการคำนวณอิงเวลาในไฟล์ ไม่ได้บีบเวลาเหมือน REPLAY_SPEED

ข้อสมมติที่ต้องระบุ: ไฟล์ capture บันทึกเฉพาะ "สิ่งที่พบ" ไม่ได้บันทึกช่วงที่แหล่งข้อมูลล่ม
ระบบจึงถือว่าข้อมูลฟ้าผ่าพร้อมตลอดช่วงตั้งแต่เริ่มบันทึก (ดูจากชื่อไฟล์) ถึงหลัง strike
สุดท้าย 35 นาที เช่น พายุ 16 ก.ย. มีช่วงรีสตาร์ทเซิร์ฟเวอร์ราว 2 นาทีที่ไม่ได้บันทึก
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from . import config, db
from .geo import haversine_km
from .risk import DATA_WINDOW_MINUTES, RADAR_WINDOW_MINUTES, risk_for, summarize
from .sources.replay import load_capture
from .timeutil import parse_iso, to_iso

router = APIRouter(prefix="/api/replays", tags=["replay"])

_STARTED_AT = re.compile(r"capture_(\d{8}T\d{6}Z)")
_THAI = timezone(timedelta(hours=7))


class CaptureTimeline:
    """ไฟล์ capture หนึ่งไฟล์ ที่ถามได้ว่า "ณ เวลา t มีอะไรเกิดขึ้นแล้วบ้าง" """

    def __init__(self, path: Path) -> None:
        self.path = path
        records = load_capture(path)
        self.strikes = [(parse_iso(r["ts_iso"]), r) for r in records if r.get("kind") != "radar"]
        self.frames = [(parse_iso(r["frame_ts"]), r) for r in records if r.get("kind") == "radar"]
        moments = [t for t, _ in self.strikes] + [t for t, _ in self.frames]
        if not moments:
            raise ValueError(f"{path.name} ไม่มี record ที่อ่านได้")

        self.first_record = min(moments)
        self.last_record = max(moments)
        match = _STARTED_AT.search(path.name)
        started = (
            datetime.strptime(match.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc) if match else None
        )
        self.capture_start = min(started, self.first_record) if started else self.first_record
        # ช่วงที่ให้เลื่อนดู: ก่อนเหตุการณ์แรก 20 นาที ถึงหลังเหตุการณ์สุดท้ายจนนาฬิกา all-clear ครบ
        self.view_start = max(self.capture_start, self.first_record - timedelta(minutes=20))
        self.view_end = self.last_record + timedelta(minutes=config.ALL_CLEAR_MINUTES + 5)

    # -- ความพร้อมของข้อมูล ณ เวลา t ---------------------------------------

    @property
    def sparse_lightning(self) -> bool:
        """ไฟล์ที่ฟ้าผ่ามาจาก Blitzortung ล้วน ใช้กฎเดียวกับโหมดสด (ต้องมีเรดาร์ยืนยันก่อน ALL_CLEAR)"""
        sources = {record.get("source") for _, record in self.strikes}
        return bool(sources) and sources <= {"blitzortung"}

    def lightning_availability(self, t: datetime) -> tuple[bool, str | None]:
        if not self.strikes:
            return False, "ไฟล์นี้ไม่มีข้อมูลฟ้าผ่า"
        if t < self.capture_start:
            return False, "ก่อนช่วงเวลาที่เริ่มบันทึกข้อมูล"
        if t > self.view_end:
            return False, "หลังช่วงเวลาที่บันทึกข้อมูลไว้"
        return True, None

    def radar_availability(self, t: datetime) -> tuple[bool, str | None]:
        if not self.frames:
            return False, "ไฟล์นี้บันทึกก่อนเพิ่มโมดูลเรดาร์ จึงไม่มีข้อมูลเรดาร์"
        latest = self.latest_frame(t)
        if latest is None:
            return False, "ยังไม่ถึงภาพเรดาร์ภาพแรกในไฟล์"
        age = (t - latest[0]).total_seconds() / 60
        if age > config.RADAR_STALE_MINUTES:
            return False, f"ภาพเรดาร์ในไฟล์ขาดช่วง {age:.0f} นาที"
        return True, None

    # -- ข้อมูล ณ เวลา t -----------------------------------------------------

    def latest_frame(self, t: datetime) -> tuple[datetime, dict] | None:
        past = [item for item in self.frames if item[0] <= t]
        return past[-1] if past else None

    def strikes_near(self, stadium: dict, t: datetime, minutes: float) -> list[dict]:
        """strike ที่เกิดใน N นาทีก่อน t และอยู่ในรัศมีเก็บข้อมูลของสนาม (ใหม่ไปเก่า)"""
        since = t - timedelta(minutes=minutes)
        found = []
        for moment, record in self.strikes:
            if not since <= moment <= t:
                continue
            distance = _distance(stadium, record)
            if distance <= config.INGEST_RADIUS_KM:
                found.append({
                    "ts_iso": record["ts_iso"],
                    "lat": record["lat"],
                    "lon": record["lon"],
                    "distance_km": round(distance, 3),
                    "source": record.get("source", "?"),
                })
        found.sort(key=lambda s: s["ts_iso"], reverse=True)
        return found

    def radar_history(self, stadium_id: int, t: datetime, minutes: float) -> list[dict]:
        since = t - timedelta(minutes=minutes)
        history = []
        for moment, record in reversed(self.frames):
            if not since <= moment <= t:
                continue
            for exposure in record.get("exposures") or []:
                if exposure.get("stadium_id") == stadium_id:
                    history.append({**exposure, "frame_ts": record["frame_ts"]})
        return history

    # -- สรุปไฟล์ --------------------------------------------------------------

    def describe(self, stadiums: list[dict]) -> dict:
        affected = []
        peak = None
        for stadium in stadiums:
            distances = [(_distance(stadium, r), moment) for moment, r in self.strikes]
            near = [(d, m) for d, m in distances if d <= config.INGEST_RADIUS_KM]
            if not near:
                continue
            closest_km, closest_at = min(near)
            affected.append({
                "stadium_id": stadium["id"],
                "club": stadium["club"],
                "name": stadium["name"],
                "strikes": len(near),
                "closest_km": round(closest_km, 2),
                "closest_at": to_iso(closest_at),
            })
            if peak is None or closest_km < peak["closest_km"]:
                peak = affected[-1]
        affected.sort(key=lambda a: a["closest_km"])

        start_local = self.first_record.astimezone(_THAI)
        return {
            "file": self.path.name,
            "label": f"พายุจริง {start_local:%d/%m/%Y %H:%M} น." if self.strikes else f"เรดาร์ {start_local:%d/%m/%Y %H:%M} น.",
            "capture_start": to_iso(self.capture_start),
            "first_record": to_iso(self.first_record),
            "last_record": to_iso(self.last_record),
            "view_start": to_iso(self.view_start),
            "view_end": to_iso(self.view_end),
            "strikes": len(self.strikes),
            "radar_frames": len(self.frames),
            "sources": sorted({r.get("source", "?") for _, r in self.strikes}),
            "stadiums": affected,
            "peak": peak,
        }


def _distance(stadium: dict, record: dict) -> float:
    # ใช้ระยะที่ Xweather คำนวณให้เมื่อจุด query คือตัวสนามนี้ (เหมือน ingest.py)
    ref_lat, ref_lon, ref_km = record.get("ref_lat"), record.get("ref_lon"), record.get("ref_distance_km")
    if (
        ref_km is not None and ref_lat is not None and ref_lon is not None
        and abs(ref_lat - stadium["lat"]) < 1e-6 and abs(ref_lon - stadium["lon"]) < 1e-6
    ):
        return float(ref_km)
    return haversine_km(stadium["lat"], stadium["lon"], float(record["lat"]), float(record["lon"]))


# --- โหลดไฟล์แบบมี cache (ไฟล์ที่ยังถูกเขียนอยู่จะโหลดใหม่เมื่อเปลี่ยน) --------

_cache: dict[str, tuple[float, CaptureTimeline]] = {}


def timeline(name: str) -> CaptureTimeline:
    # รับเฉพาะชื่อไฟล์ .jsonl ใน data/captures - กันการอ่านไฟล์อื่นผ่าน path แปลก ๆ
    if Path(name).name != name or not name.endswith(".jsonl"):
        raise HTTPException(404, "ไม่พบไฟล์ replay")
    path = config.CAPTURE_DIR / name
    if not path.is_file():
        raise HTTPException(404, "ไม่พบไฟล์ replay")
    mtime = path.stat().st_mtime
    cached = _cache.get(name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        loaded = CaptureTimeline(path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    _cache[name] = (mtime, loaded)
    return loaded


def _moment(tl: CaptureTimeline, t: str | None) -> datetime:
    if not t:
        return tl.view_start
    moment = parse_iso(t)
    if moment is None:
        raise HTTPException(422, "รูปแบบเวลา t ไม่ถูกต้อง (ใช้ ISO-8601 เช่น 2026-09-16T06:30:00Z)")
    return min(max(moment, tl.view_start), tl.view_end)


# --- Endpoints -------------------------------------------------------------


@router.get("", summary="รายการไฟล์พายุจริงที่บันทึกไว้")
def list_replays() -> dict:
    stadiums = db.list_stadiums(active_only=True)
    items = []
    for path in sorted(config.CAPTURE_DIR.glob("*.jsonl")):
        try:
            items.append(timeline(path.name).describe(stadiums))
        except HTTPException:
            continue
    # ไฟล์ที่มีฟ้าผ่ามากที่สุดขึ้นก่อน - มักเป็นเหตุการณ์ที่เหมาะกับการ demo
    items.sort(key=lambda d: (-d["strikes"], d["file"]))
    return {"count": len(items), "replays": items}


@router.get("/{name}/risk", summary="สถานะทุกสนาม ณ เวลาเสมือน t")
def replay_risk(name: str, t: str | None = Query(None, description="เวลา UTC แบบ ISO-8601")) -> dict:
    tl = timeline(name)
    now = _moment(tl, t)
    lightning = tl.lightning_availability(now)
    radar = tl.radar_availability(now)
    stadiums = db.list_stadiums(active_only=True)
    results = [
        risk_for(
            stadium,
            tl.strikes_near(stadium, now, DATA_WINDOW_MINUTES),
            True,
            lightning,
            radar,
            tl.radar_history(stadium["id"], now, RADAR_WINDOW_MINUTES),
            now=now,
            lightning_sparse=tl.sparse_lightning,
        )
        for stadium in stadiums
    ]
    results, summary = summarize(results)
    return {
        "file": name,
        "virtual_now": to_iso(now),
        "count": len(results),
        "summary": summary,
        "lightning_available": lightning[0],
        "lightning_unavailable_reason": lightning[1],
        "radar_available": radar[0],
        "radar_unavailable_reason": radar[1],
        "results": results,
    }


@router.get("/{name}/strikes", summary="strike รอบสนาม ณ เวลาเสมือน t")
def replay_strikes(
    name: str,
    stadium_id: int = Query(...),
    t: str | None = Query(None),
    minutes: int = Query(30, ge=1, le=180),
) -> dict:
    tl = timeline(name)
    now = _moment(tl, t)
    stadium = db.get_stadium(stadium_id)
    if stadium is None:
        raise HTTPException(404, f"ไม่พบสนาม id={stadium_id}")
    strikes = tl.strikes_near(stadium, now, minutes)
    return {"stadium_id": stadium_id, "virtual_now": to_iso(now), "count": len(strikes), "strikes": strikes}


@router.get("/{name}/radar", summary="cell ฝนแรงจากภาพเรดาร์ล่าสุด ณ เวลาเสมือน t")
def replay_radar(name: str, t: str | None = Query(None)) -> dict:
    tl = timeline(name)
    now = _moment(tl, t)
    ok, reason = tl.radar_availability(now)
    latest = tl.latest_frame(now)
    record = latest[1] if latest else None
    return {
        "available": ok,
        "unavailable_reason": reason,
        "virtual_now": to_iso(now),
        "frame": {k: record[k] for k in ("frame_ts", "source", "tiles_ok", "tiles_total", "max_dbz")} if record else None,
        "hotspot_dbz": config.RADAR_HOTSPOT_DBZ,
        "cells": list(record.get("cells") or []) if record else [],
        # ภาพเรดาร์ดิบของวันที่บันทึกหมดอายุไปแล้ว (RainViewer เก็บ 2 ชม.) จึงมีแต่ผลวิเคราะห์
        "tile_url_template": None,
        "tile_max_zoom": config.RADAR_ZOOM,
    }
