"""FastAPI app: REST API + เสิร์ฟ frontend + เปิดตัวรับข้อมูลตอน startup

รัน:  uvicorn app.main:app --reload
แล้วเปิด http://127.0.0.1:8000

ข้อมูลเข้า 2 ทาง แยกกันและตรวจความพร้อมแยกกัน:
  * ฟ้าผ่า  (LIGHTNING_SOURCE)  -> หลักฐานสั่งหยุด (SUSPEND / DANGER)
  * เรดาร์ (RainViewer)        -> สัญญาณเตือนล่วงหน้า (WATCH)
ทั้งสองถูกรวมเป็นสถานะเดียวใน app/model/fusion.py (ประกอบผลใน app/risk.py)

โหมด replay บนหน้าเว็บอยู่ใน app/replay_view.py (ไม่ต้องรีสตาร์ท ไม่แตะข้อมูลสด)

ลำดับตอนเริ่มระบบ
  1. สร้างตาราง และ upsert สนามจาก GeoJSON ทุกครั้ง (แก้พิกัดแล้วรีสตาร์ตก็มีผล)
  2. สร้างแหล่งข้อมูลฟ้าผ่าตาม LIGHTNING_SOURCE
  3. เปิดตัวรับฟ้าผ่า และตัวดึงเรดาร์ เป็น background task
     (ตอน LIGHTNING_SOURCE=replay เรดาร์มาจากไฟล์เดียวกัน ไม่ดึงสด)
  4. เปิดงานล้างข้อมูลเก่าทุก 60 วินาที
"""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import config, db, seed_db
from .ingest import Ingestor
from .model.fusion import RISK_ORDER
from .model.tiering import rings
from .replay_view import router as replay_router
from .risk import DATA_WINDOW_MINUTES, RADAR_WINDOW_MINUTES, risk_for, summarize
from .sources import LightningSource, build_source
from .sources.rainviewer import ATTRIBUTION, RainViewerRadar

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
log = logging.getLogger("app")

# httpx log URL เต็มที่ระดับ INFO ซึ่งมี client_secret อยู่ใน query string
# ต้องปิดไว้ ไม่งั้น credential จะไปโผล่ใน log ของเซิร์ฟเวอร์ทุกรอบ poll
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# ใช้ใน /api/meta และเอกสาร (ไม่ได้แสดงเป็นแถบบนหน้าเว็บแล้ว)
DISCLAIMER = (
    "ต้นแบบเชิงวิชาการ — ไม่ใช่ระบบเตือนภัยสำหรับตัดสินใจจริง "
    "ตำแหน่งฟ้าผ่ามีความคลาดเคลื่อนตามข้อจำกัดของเครือข่ายตรวจจับที่ใช้"
)


class Runtime:
    """สถานะของตัวรับข้อมูลที่ API ต้องอ่าน"""

    source: LightningSource | None = None
    ingestor: Ingestor | None = None
    source_error: str | None = None
    radar: RainViewerRadar | None = None
    radar_error: str | None = None


runtime = Runtime()


async def _run_source(source: LightningSource, ingestor: Ingestor) -> None:
    try:
        await source.run(ingestor)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - แสดงบน UI แทนการทำให้ทั้ง app ล่ม
        runtime.source_error = str(exc)
        log.error("แหล่งข้อมูลฟ้าผ่า %s หยุดทำงาน: %s", source.name, exc)


async def _run_radar(radar: RainViewerRadar, ingestor: Ingestor) -> None:
    try:
        await radar.run(ingestor.ingest_radar)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        runtime.radar_error = str(exc)
        log.error("โมดูลเรดาร์หยุดทำงาน: %s", exc)


async def _cleanup_loop() -> None:
    while True:
        await asyncio.to_thread(db.cleanup_strikes)
        await asyncio.to_thread(db.cleanup_radar)
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.init_db()
    # upsert จาก GeoJSON ทุกครั้งที่เปิด: แก้พิกัดในไฟล์แล้วรีสตาร์ตก็มีผลทันที ไม่ต้องลบฐานข้อมูล
    seed_db.seed()

    tasks: list[asyncio.Task] = [asyncio.create_task(_cleanup_loop())]
    source: LightningSource | None = None
    try:
        source = build_source()
    except RuntimeError as exc:
        # ตั้งค่าผิด (เช่น ไม่มี credential / ไม่มีไฟล์ replay) - เปิด API ได้แต่แจ้งชัด ๆ
        runtime.source_error = str(exc)
        log.error("สร้างแหล่งข้อมูลฟ้าผ่าไม่สำเร็จ: %s", exc)

    replaying = source is not None and source.name == "replay"
    if replaying:
        # ไม่ให้ข้อมูลที่เล่นซ้ำปนกับข้อมูลสดที่อาจค้างอยู่ใน DB
        db.clear_strikes()
        db.clear_radar()
    # capture เฉพาะข้อมูลที่รับสด - ไม่บันทึกซ้ำตอน replay
    runtime.ingestor = Ingestor(capture=config.CAPTURE_ENABLED and not replaying)

    if source is not None:
        runtime.source = source
        log.info("แหล่งข้อมูลฟ้าผ่า: %s", source.label)
        tasks.append(asyncio.create_task(_run_source(source, runtime.ingestor)))

    if replaying:
        source.radar_sink = runtime.ingestor.ingest_radar  # เรดาร์มาจากไฟล์เดียวกัน
    elif config.RADAR_ENABLED:
        try:
            runtime.radar = RainViewerRadar()
        except Exception as exc:  # noqa: BLE001 - เช่น ไม่พบไฟล์ตารางสี
            runtime.radar_error = str(exc)
            log.error("เริ่มโมดูลเรดาร์ไม่สำเร็จ: %s", exc)
        else:
            tasks.append(asyncio.create_task(_run_radar(runtime.radar, runtime.ingestor)))

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task


app = FastAPI(
    title="GeoAI-for-Disaster - เฝ้าระวังฟ้าผ่าสนามไทยลีก 1",
    description=(
        "ติดตามฟ้าผ่าและเรดาร์ฝนรอบสนามไทยลีก 1 ฤดูกาล 2026-27 พร้อม nowcast ระยะสั้น\n\n"
        f"{DISCLAIMER}\n\n{ATTRIBUTION}"
    ),
    version="0.5.0",
    lifespan=lifespan,
)

# เปิดกว้างเพราะเป็น MVP สำหรับ local dev - โปรดจำกัด origin ก่อนขึ้น production
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["GET"], allow_headers=["*"])
app.include_router(replay_router)


# --- ความพร้อมของข้อมูล -----------------------------------------------------


def _lightning_availability() -> tuple[bool, str | None]:
    """แหล่งข้อมูลฟ้าผ่าพร้อมใช้หรือไม่ - ไม่พร้อม = ห้ามตอบ ALL_CLEAR"""
    if runtime.source_error:
        return False, runtime.source_error
    if runtime.source is None:
        return False, "ยังไม่ได้ตั้งค่าแหล่งข้อมูลฟ้าผ่า"
    return runtime.source.availability()


def _lightning_sparse() -> bool:
    """ข้อมูลฟ้าผ่าที่ใช้อยู่มาจากเครือข่ายที่ตรวจจับในไทยได้ไม่ครบหรือไม่ (เช่น Blitzortung อย่างเดียว)"""
    return runtime.source.coverage_sparse() if runtime.source else False


def _radar_availability() -> tuple[bool, str | None]:
    if runtime.source is not None and runtime.source.name == "replay":
        return runtime.source.radar_availability()
    if not config.RADAR_ENABLED:
        return False, "ปิดโมดูลเรดาร์ไว้ (RADAR_ENABLED=false)"
    if runtime.radar_error:
        return False, runtime.radar_error
    if runtime.radar is None:
        return False, "ยังไม่ได้เริ่มโมดูลเรดาร์"
    return runtime.radar.availability()


def _monitored_ids() -> set[int] | None:
    """id สนามที่แหล่งข้อมูลฟ้าผ่าเฝ้าอยู่ (None = ทุกสนาม active)"""
    return runtime.source.monitored_stadium_ids() if runtime.source else None


def _stadium_public(stadium: dict) -> dict:
    return {
        **{key: stadium[key] for key in ("id", "club", "name", "lat", "lon", "capacity", "status", "note")},
        # ติดธงให้ UI เห็นชัดว่าพิกัดนี้ยังไม่ได้ยืนยัน (ดู app/seed_db.py)
        "location_unverified": stadium["note"] == "verify",
    }


# --- Endpoints -------------------------------------------------------------


@app.get("/api/meta", summary="แหล่งข้อมูลปัจจุบัน ค่าคงที่ และ disclaimer")
def get_meta() -> dict:
    source = runtime.source
    watched = _monitored_ids()
    lightning_ok, lightning_reason = _lightning_availability()
    radar_ok, radar_reason = _radar_availability()
    latest_frame = db.latest_radar_frame()
    return {
        "source": source.name if source else None,
        "source_label": source.label if source else "ไม่มีแหล่งข้อมูลฟ้าผ่า",
        "is_replay": bool(source and source.name == "replay"),
        "lightning_available": lightning_ok,
        "lightning_unavailable_reason": lightning_reason,
        "lightning_sparse": _lightning_sparse(),
        "radar_available": radar_ok,
        "radar_unavailable_reason": radar_reason,
        "radar_frame_ts": latest_frame["frame_ts"] if latest_frame else None,
        "radar_hotspot_dbz": config.RADAR_HOTSPOT_DBZ,
        "radar_attribution": ATTRIBUTION,
        "monitored_stadium_ids": sorted(watched) if watched else None,
        "source_status": source.status() if source else {},
        "source_error": runtime.source_error,
        "radar_status": runtime.radar.status() if runtime.radar else {},
        "ingest": runtime.ingestor.status() if runtime.ingestor else {},
        "rings": rings(),
        "status_order": RISK_ORDER,
        "all_clear_minutes": config.ALL_CLEAR_MINUTES,
        "ingest_radius_km": config.INGEST_RADIUS_KM,
        "nowcast_window_minutes": config.NOWCAST_WINDOW_MINUTES,
        "disclaimer": DISCLAIMER,
    }


@app.get("/api/stadiums", summary="รายชื่อสนามทั้งหมด")
def get_stadiums(active_only: bool = Query(False, description="เอาเฉพาะสนามที่ใช้งานจริง")) -> dict:
    stadiums = db.list_stadiums(active_only=active_only)
    return {"count": len(stadiums), "stadiums": [_stadium_public(s) for s in stadiums]}


@app.get("/api/risk", summary="ระดับความเสี่ยงของสนาม (รวมฟ้าผ่า + เรดาร์)")
def get_risk(
    stadium_id: int | None = Query(None, description="ระบุ id = สนามเดียว, เว้นว่าง = ทุกสนามที่ active"),
) -> dict:
    lightning = _lightning_availability()
    radar = _radar_availability()
    watched = _monitored_ids()
    radar_history = db.radar_exposures_since(RADAR_WINDOW_MINUTES)

    if stadium_id is not None:
        stadium = db.get_stadium(stadium_id)
        if stadium is None:
            raise HTTPException(404, f"ไม่พบสนาม id={stadium_id}")
        monitored = stadium["status"] == "current" and (watched is None or stadium_id in watched)
        return risk_for(
            stadium, db.recent_strikes(stadium_id, DATA_WINDOW_MINUTES), monitored,
            lightning, radar, radar_history.get(stadium_id, []),
            lightning_sparse=_lightning_sparse(),
        )

    sparse = _lightning_sparse()
    stadiums = db.list_stadiums(active_only=True)
    grouped = db.recent_strikes_bulk(DATA_WINDOW_MINUTES)
    results, summary = summarize([
        risk_for(
            s, grouped.get(s["id"], []), watched is None or s["id"] in watched,
            lightning, radar, radar_history.get(s["id"], []),
            lightning_sparse=sparse,
        )
        for s in stadiums
    ])
    return {
        "count": len(results),
        "summary": summary,
        "lightning_available": lightning[0],
        "lightning_unavailable_reason": lightning[1],
        "radar_available": radar[0],
        "radar_unavailable_reason": radar[1],
        "results": results,
    }


@app.get("/api/strikes/recent", summary="strike ล่าสุดรอบสนามหนึ่งแห่ง")
def get_recent_strikes(
    stadium_id: int = Query(..., description="id ของสนาม"),
    minutes: int = Query(30, ge=1, le=180, description="ย้อนหลังกี่นาที"),
) -> dict:
    stadium = db.get_stadium(stadium_id)
    if stadium is None:
        raise HTTPException(404, f"ไม่พบสนาม id={stadium_id}")
    strikes = db.recent_strikes(stadium_id, minutes)
    return {"stadium_id": stadium_id, "name": stadium["name"], "minutes": minutes, "count": len(strikes), "strikes": strikes}


@app.get("/api/strikes/region", summary="ฟ้าผ่าจริงทั่วพื้นที่สนามไทย (ก่อนกรองระยะจากสนาม)")
def get_region_strikes(
    minutes: int = Query(60, ge=1, le=180, description="ย้อนหลังกี่นาที"),
) -> dict:
    strikes = runtime.ingestor.region_strikes(minutes) if runtime.ingestor else []
    return {"minutes": minutes, "count": len(strikes), "strikes": strikes}


@app.get("/api/radar", summary="ภาพเรดาร์ล่าสุด: cell ฝนแรง และ tile สำหรับแสดงบนแผนที่")
def get_radar() -> dict:
    radar_ok, radar_reason = _radar_availability()
    frame = db.latest_radar_frame()
    return {
        "available": radar_ok,
        "unavailable_reason": radar_reason,
        "frame": frame,
        "hotspot_dbz": config.RADAR_HOTSPOT_DBZ,
        "cells": db.radar_cells_for(frame["frame_ts"]) if frame else [],
        # tile ภาพสดมีเฉพาะตอนดึงจาก RainViewer (ภาพเก่าหมดอายุหลัง 2 ชม. มีแต่ cell)
        "tile_url_template": runtime.radar.latest_tile_template if runtime.radar else None,
        "tile_max_zoom": config.RADAR_ZOOM,
        "attribution": ATTRIBUTION,
    }


@app.get("/api/health", summary="ตรวจสุขภาพระบบ")
def get_health() -> dict:
    lightning_ok, lightning_reason = _lightning_availability()
    radar_ok, radar_reason = _radar_availability()
    return {
        "ok": lightning_ok and radar_ok,
        "source": runtime.source.name if runtime.source else None,
        "lightning_available": lightning_ok,
        "lightning_unavailable_reason": lightning_reason,
        "radar_available": radar_ok,
        "radar_unavailable_reason": radar_reason,
        "stadiums_active": len(db.list_stadiums(active_only=True)),
        "strikes_stored": db.count_strikes(),
        "source_status": runtime.source.status() if runtime.source else {},
        "radar_status": runtime.radar.status() if runtime.radar else {},
        "source_error": runtime.source_error,
        "ingest": runtime.ingestor.status() if runtime.ingestor else {},
    }


# mount ท้ายสุดเสมอ: StaticFiles ที่ "/" จะกลืน path ทั้งหมดที่เหลือ
app.mount("/", StaticFiles(directory=config.STATIC_DIR, html=True), name="static")
