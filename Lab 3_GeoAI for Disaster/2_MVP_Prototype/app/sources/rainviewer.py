"""แหล่งข้อมูลเรดาร์ฝน: RainViewer (รวมเรดาร์จากหลายประเทศ รวมถึงไทย)

ข้อจำกัดของ free tier (ตรวจสอบกับของจริง 2026-09-17):
  * ใช้ได้เฉพาะงานส่วนตัว/การศึกษา และต้องให้เครดิต RainViewer พร้อมลิงก์
  * ภาพย้อนหลัง 2 ชม. ทุก 10 นาที - ไม่มีภาพพยากรณ์ (nowcast ของเขาถูกยกเลิก ม.ค. 2026)
  * zoom สูงสุด 7 (~1.2 กม./pixel ที่ละติจูดไทย) zoom สูงกว่านั้นได้ภาพ "Zoom Level Not Supported"
  * palette เดียว (Universal Blue) และจำกัด 100 คำขอ/IP/นาที
  * ไม่รับประกันความพร้อมของข้อมูล - เรดาร์บางตัวอาจหายไปได้

ต่อรอบ: ดึงรายการภาพ -> ภาพใหม่แต่ละภาพโหลด tile ที่ครอบคลุมสนาม (8 tile สำหรับ 16 สนาม)
-> แปลงเป็น dBZ -> หา cell และวัดระยะถึงสนาม (app/model/radar_cells.py) -> ส่งให้ ingest
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import httpx

from .. import config
from ..model.radar_cells import DbzGrid, detect_cells, load_palette, stadium_exposure, tiles_covering
from ..timeutil import parse_iso, to_iso, utcnow

log = logging.getLogger(__name__)

ATTRIBUTION = "Radar data © RainViewer (https://www.rainviewer.com)"


@dataclass
class RadarFrame:
    """ผลวิเคราะห์ภาพเรดาร์หนึ่งภาพ - สิ่งที่เก็บลง DB และ capture (ไม่เก็บภาพดิบ)"""

    frame_ts: str
    source: str
    tiles_ok: int
    tiles_total: int
    max_dbz: int | None
    cells: list[dict] = field(default_factory=list)
    exposures: list[dict] = field(default_factory=list)

    def to_record(self) -> dict:
        return {"kind": "radar", **asdict(self)}

    @classmethod
    def from_record(cls, record: dict, frame_ts: str | None = None, source: str | None = None) -> RadarFrame:
        return cls(
            frame_ts=frame_ts or record["frame_ts"],
            source=source or record.get("source", "?"),
            tiles_ok=int(record.get("tiles_ok", 0)),
            tiles_total=int(record.get("tiles_total", 0)),
            max_dbz=record.get("max_dbz"),
            cells=list(record.get("cells") or []),
            exposures=list(record.get("exposures") or []),
        )


OnRadarFrame = Callable[[RadarFrame], object]


def build_frame(grid: DbzGrid, frame_ts: str, source: str, tiles_total: int, stadiums: list[dict]) -> RadarFrame:
    """ภาพเรดาร์ที่แปลงเป็น dBZ แล้ว -> cell + ระยะถึงแต่ละสนาม"""
    cells = detect_cells(grid, config.RADAR_HOTSPOT_DBZ, config.RADAR_MIN_CELL_PIXELS)
    exposures = [
        stadium_exposure(
            grid,
            stadium["id"],
            stadium["lat"],
            stadium["lon"],
            threshold=config.RADAR_HOTSPOT_DBZ,
            watch_km=config.RING_WATCH_KM,
            search_km=config.RADAR_SEARCH_KM,
        ).to_dict()
        for stadium in stadiums
    ]
    return RadarFrame(
        frame_ts=frame_ts,
        source=source,
        tiles_ok=grid.tile_count,
        tiles_total=tiles_total,
        max_dbz=grid.max_dbz(),
        cells=[cell.to_dict() for cell in cells],
        exposures=exposures,
    )


class RainViewerRadar:
    name = "rainviewer"
    label = "RAINVIEWER RADAR"

    def __init__(self) -> None:
        self.palette = load_palette(config.RADAR_PALETTE_PATH.read_text(encoding="utf-8"))
        self._done: set[str] = set()
        self.latest_frame_ts: str | None = None
        self.latest_tile_template: str | None = None
        self.tiles_per_frame = 0
        self.frames_processed = 0
        self.requests_total = 0
        self.last_check_at: str | None = None
        self.last_error: str | None = None

    # -- ความพร้อมของข้อมูล -------------------------------------------------

    def availability(self) -> tuple[bool, str | None]:
        if self.latest_frame_ts is None:
            return False, self.last_error or "กำลังโหลดภาพเรดาร์ชุดแรก"
        frame_time = parse_iso(self.latest_frame_ts)
        age_minutes = (utcnow() - frame_time).total_seconds() / 60 if frame_time else 1e9
        if age_minutes > config.RADAR_STALE_MINUTES:
            return False, f"ภาพเรดาร์ล่าสุดเก่า {age_minutes:.0f} นาที"
        return True, None

    def status(self) -> dict:
        available, reason = self.availability()
        return {
            "available": available,
            "unavailable_reason": reason,
            "latest_frame_ts": self.latest_frame_ts,
            "tiles_per_frame": self.tiles_per_frame,
            "frames_processed": self.frames_processed,
            "requests_total": self.requests_total,
            "last_check_at": self.last_check_at,
            "last_error": self.last_error,
            "hotspot_dbz": config.RADAR_HOTSPOT_DBZ,
            "attribution": ATTRIBUTION,
        }

    # -- งานหลัก ------------------------------------------------------------

    async def run(self, on_frame: OnRadarFrame) -> None:
        from .. import db  # import ตอนใช้ เลี่ยง circular import

        headers = {"User-Agent": "GeoAI-lightning-lab/0.3 (educational use)"}
        async with httpx.AsyncClient(timeout=30.0, headers=headers) as http:
            while True:
                try:
                    await self._poll_once(http, on_frame, db)
                    self.last_error = None
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 - รอบหน้าต้องยังเดินต่อ
                    self.last_error = f"{type(exc).__name__}: {exc}"
                    log.warning("ดึงภาพเรดาร์ไม่สำเร็จ: %s", self.last_error)
                self.last_check_at = to_iso(utcnow())
                await asyncio.sleep(max(60, config.RADAR_POLL_SECONDS))

    async def _poll_once(self, http: httpx.AsyncClient, on_frame: OnRadarFrame, db) -> None:
        response = await http.get(config.RADAR_MAPS_URL)
        self.requests_total += 1
        response.raise_for_status()
        maps = response.json()
        host = maps["host"]
        frames = sorted(maps["radar"]["past"], key=lambda f: f["time"])
        if not frames:
            raise RuntimeError("RainViewer ไม่มีภาพเรดาร์ในรายการ")

        stadiums = await asyncio.to_thread(db.list_stadiums, True)
        tiles = sorted(
            tiles_covering(((s["lat"], s["lon"]) for s in stadiums), config.RADAR_SEARCH_KM, config.RADAR_ZOOM)
        )
        self.tiles_per_frame = len(tiles)
        # tile ไว้แสดงบนแผนที่ (เปิด smoothing ให้ดูสวย ต่างจากตอนวิเคราะห์ที่ต้องปิด)
        self.latest_tile_template = f"{host}{frames[-1]['path']}/256/{{z}}/{{x}}/{{y}}/2/1_1.png"

        for frame in frames:
            frame_ts = to_iso(datetime.fromtimestamp(frame["time"], tz=timezone.utc))
            if frame_ts in self._done or await asyncio.to_thread(db.radar_frame_exists, frame_ts):
                self._remember(frame_ts)
                continue

            grid = DbzGrid(config.RADAR_ZOOM)
            for tx, ty in tiles:
                url = f"{host}{frame['path']}/256/{config.RADAR_ZOOM}/{tx}/{ty}/2/0_0.png"
                tile = await http.get(url)
                self.requests_total += 1
                if tile.status_code == 200:
                    self._add_tile(grid, tx, ty, tile.content)
                await asyncio.sleep(config.RADAR_REQUEST_GAP_SECONDS)  # ไม่เกิน 100 คำขอ/นาที

            if grid.tile_count == 0:
                raise RuntimeError(f"โหลด tile ของภาพ {frame_ts} ไม่ได้เลย")
            result = build_frame(grid, frame_ts, self.name, len(tiles), stadiums)
            await asyncio.to_thread(on_frame, result)
            self.frames_processed += 1
            self._remember(frame_ts)
            log.info(
                "เรดาร์ %s: dBZ สูงสุด %s, cell >= %s dBZ %s จุด",
                frame_ts, result.max_dbz, config.RADAR_HOTSPOT_DBZ, len(result.cells),
            )

    def _add_tile(self, grid: DbzGrid, tx: int, ty: int, content: bytes) -> None:
        from PIL import Image

        before = grid.unmatched_pixels
        image = Image.open(io.BytesIO(content)).convert("RGBA")
        grid.add_tile(tx, ty, image.getdata(), self.palette)
        # สีไม่ตรงตารางจำนวนมาก = ได้ภาพเตือน (เช่น zoom เกินที่ free tier รองรับ) ไม่ใช่ภาพเรดาร์
        if grid.unmatched_pixels - before > 1000:
            raise RuntimeError(
                f"tile {tx},{ty} สีไม่ตรงตาราง dBZ - ตรวจ RADAR_ZOOM (free tier สูงสุด 7)"
            )

    def _remember(self, frame_ts: str) -> None:
        self._done.add(frame_ts)
        if self.latest_frame_ts is None or frame_ts > self.latest_frame_ts:
            self.latest_frame_ts = frame_ts
