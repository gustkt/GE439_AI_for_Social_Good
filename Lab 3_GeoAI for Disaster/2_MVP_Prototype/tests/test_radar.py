"""ทดสอบการวิเคราะห์ภาพเรดาร์แบบ offline (ไม่ต่อเครือข่าย)

รัน:  python -m tests.test_radar

ภาพ tile ในเทสต์นี้สร้างขึ้นเอง (fixture) เพื่อให้รู้คำตอบที่ถูกต้องล่วงหน้า
เช่น วาง cell 45 dBZ ขนาด 3x3 pixel ไว้ตรงตำแหน่งที่รู้ แล้วตรวจว่าระบบหาเจอ
ถูกตำแหน่ง ถูกขนาด - เป็นการทดสอบสูตร ไม่ใช่ข้อมูลที่ระบบใช้จริง
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from datetime import timedelta
from pathlib import Path

from app import config
from app.geo import haversine_km, offset_km
from app.model.fusion import fuse
from app.model.radar_cells import (
    TILE_SIZE,
    DbzGrid,
    detect_cells,
    load_palette,
    lonlat_to_pixel,
    pixel_size_km,
    pixel_to_lonlat,
    stadium_exposure,
    tiles_covering,
)
from app.sources.rainviewer import RadarFrame, build_frame
from app.sources.replay import ReplaySource, load_capture
from app.timeutil import parse_iso, to_iso, utcnow

_passed: list[str] = []

PALETTE = load_palette(config.RADAR_PALETTE_PATH.read_text(encoding="utf-8"))
COLOUR = {dbz: rgba for rgba, dbz in PALETTE.items()}
TRANSPARENT = (0, 0, 0, 0)
ZOOM = 7
TILE = (99, 59)  # ภาคกลางของไทย


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} ล้มเหลว {detail}")
    _passed.append(name)


def make_grid(paint: dict[tuple[int, int], int], extra: dict[tuple[int, int], tuple] | None = None) -> DbzGrid:
    """สร้าง tile สังเคราะห์: paint = {(x, y ใน tile): dBZ}"""
    pixels = [TRANSPARENT] * (TILE_SIZE * TILE_SIZE)
    for (x, y), dbz in paint.items():
        pixels[y * TILE_SIZE + x] = COLOUR[dbz]
    for (x, y), rgba in (extra or {}).items():
        pixels[y * TILE_SIZE + x] = rgba
    grid = DbzGrid(ZOOM)
    grid.add_tile(*TILE, pixels, PALETTE)
    return grid


def blob(x0: int, y0: int, size: int, dbz: int) -> dict[tuple[int, int], int]:
    return {(x, y): dbz for x in range(x0, x0 + size) for y in range(y0, y0 + size)}


def blob_centre(x0: int, y0: int, size: int) -> tuple[float, float]:
    gx = TILE[0] * TILE_SIZE + x0 + size / 2
    gy = TILE[1] * TILE_SIZE + y0 + size / 2
    return pixel_to_lonlat(gx, gy, ZOOM)


# ------------------------------------------------------------------ palette/tile


def test_palette_and_tiles() -> None:
    check("palette: โหลดตารางสีทางการได้ครบ", len(PALETTE) >= 150, str(len(PALETTE)))
    check("palette: สีส้มแดง = 45 dBZ", PALETTE.get((255, 68, 0, 255)) == 45)
    check("palette: 40 dBZ ตรงกับที่ตรวจกับภาพจริง", PALETTE.get((255, 170, 0, 255)) == 40)

    lat, lon = 13.7563, 100.5018
    back = pixel_to_lonlat(*lonlat_to_pixel(lat, lon, ZOOM), ZOOM)
    check("tile: แปลงพิกัดไป-กลับได้ค่าเดิม", abs(back[0] - lat) < 1e-9 and abs(back[1] - lon) < 1e-9)
    check("tile: ความละเอียด zoom 7 ที่ละติจูดไทย ~1.2 กม.", 1.1 < pixel_size_km(13.75, ZOOM) < 1.3)
    check("tile: กรุงเทพฯ อยู่ใน tile 99/59", TILE in tiles_covering([(lat, lon)], 10, ZOOM))


# ----------------------------------------------------------------------- cells


def test_detect_cells() -> None:
    paint = {**blob(100, 100, 3, 45), (10, 10): 50, **blob(200, 200, 2, 30)}
    grid = make_grid(paint)
    cells = detect_cells(grid, threshold=40, min_pixels=4)
    check("cell: หา cell >= 40 dBZ เจอ 1 cell", len(cells) == 1, str(cells))
    check("cell: ขนาด 9 pixel", cells[0].pixels == 9)
    check("cell: dBZ สูงสุด 45", cells[0].max_dbz == 45)
    lat, lon = blob_centre(100, 100, 3)
    check("cell: จุดศูนย์กลางถูกตำแหน่ง", haversine_km(lat, lon, cells[0].lat, cells[0].lon) < 0.1)
    check("cell: pixel เดี่ยวถูกตัดเป็นสัญญาณรบกวน",
          len(detect_cells(grid, threshold=40, min_pixels=1)) == 2)
    check("cell: ฝน 30 dBZ ไม่นับเป็น hotspot", all(c.max_dbz >= 40 for c in cells))
    check("grid: dBZ สูงสุดทั้งภาพ", grid.max_dbz() == 50)


def test_unmatched_colours() -> None:
    grid = make_grid({}, extra={(5, 5): (1, 2, 3, 255)})
    check("grid: สีไม่ตรงตารางถูกนับ ไม่ถูกเดาเป็นค่า dBZ", grid.unmatched_pixels == 1 and grid.dbz(
        TILE[0] * TILE_SIZE + 5, TILE[1] * TILE_SIZE + 5) is None)


def test_exposure() -> None:
    grid = make_grid(blob(100, 100, 3, 45))
    lat, lon = blob_centre(100, 100, 3)

    on_top = stadium_exposure(grid, 1, lat, lon, threshold=40, watch_km=16, search_km=50)
    check("exposure: สนามอยู่ใต้ cell -> ระยะใกล้ศูนย์", on_top.nearest_hotspot_km is not None
          and on_top.nearest_hotspot_km < 1.5, str(on_top))
    check("exposure: dBZ สูงสุดในวง WATCH", on_top.max_dbz_watch == 45)
    check("exposure: นับ hotspot รอบสนาม", on_top.activity_pixels == 9)

    far_lat, far_lon = offset_km(lat, lon, 30, 90)
    far = stadium_exposure(grid, 2, far_lat, far_lon, threshold=40, watch_km=16, search_km=50)
    check("exposure: cell ห่าง 30 กม. วัดระยะได้ถูก", far.nearest_hotspot_km is not None
          and abs(far.nearest_hotspot_km - 30) < 2.5, str(far.nearest_hotspot_km))
    check("exposure: cell นอกวง WATCH ไม่นับเป็น dBZ ในวง", far.max_dbz_watch is None)

    very_far = stadium_exposure(grid, 3, *offset_km(lat, lon, 80, 90), threshold=40, watch_km=16, search_km=50)
    check("exposure: นอกรัศมีค้นหา = ไม่มี hotspot", very_far.nearest_hotspot_km is None)


def test_build_frame_and_fusion() -> None:
    grid = make_grid(blob(100, 100, 3, 45))
    lat, lon = blob_centre(100, 100, 3)
    frame = build_frame(grid, "2026-09-17T10:00:00Z", "test-fixture", 1,
                        [{"id": 7, "lat": lat, "lon": lon}])
    record = frame.to_record()
    check("frame: record มี kind=radar", record["kind"] == "radar")
    check("frame: แปลงกลับจาก record ได้เหมือนเดิม", RadarFrame.from_record(record).exposures == frame.exposures)

    radar_near = frame.exposures[0]["nearest_hotspot_km"] <= config.RING_WATCH_KM
    check("fusion: cell ในวง WATCH + ไม่มี strike -> WATCH",
          fuse("ALL_CLEAR", True, radar_watch=radar_near).status == "WATCH")
    note = fuse("ALL_CLEAR", True, radar_available=False, radar_unavailable_reason="ภาพเก่า")
    check("fusion: ไม่มีเรดาร์ยัง ALL_CLEAR ได้แต่บอกเหตุผล",
          note.status == "ALL_CLEAR" and "ภาพเก่า" in " ".join(note.reasons))


# ----------------------------------------------------------------------- replay


def test_replay_with_radar() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="geoai_radar_"))
    t0 = utcnow() - timedelta(days=1)
    grid = make_grid(blob(100, 100, 3, 45))
    lat, lon = blob_centre(100, 100, 3)
    stadium = [{"id": 1, "lat": lat, "lon": lon}]
    frames = [build_frame(grid, to_iso(t0 + timedelta(minutes=m)), "rainviewer", 1, stadium) for m in (0, 10)]
    strike = {"ts_iso": to_iso(t0 + timedelta(minutes=5)), "lat": lat, "lon": lon, "source": "xweather"}
    lines = [json.dumps(frames[1].to_record()), json.dumps(strike), json.dumps(frames[0].to_record()),
             json.dumps(frames[0].to_record())]  # ซ้ำ 1 บรรทัด
    capture = tmp / "capture_fixture.jsonl"
    capture.write_text("\n".join(lines) + "\n", encoding="utf-8")

    records = load_capture(capture)
    check("replay: อ่าน record 2 ชนิดและตัดตัวซ้ำ", len(records) == 3, str(len(records)))
    check("replay: เรียงตามเวลาข้ามชนิด",
          [r.get("kind", "strike") for r in records] == ["radar", "strike", "radar"])

    replay = ReplaySource(file_name=str(capture), speed=1200)
    received_frames: list[RadarFrame] = []
    received_strikes: list = []
    replay.radar_sink = received_frames.append
    asyncio.run(asyncio.wait_for(replay.run(received_strikes.extend), timeout=15))
    check("replay: ส่งภาพเรดาร์ครบ 2 ภาพ", len(received_frames) == 2)
    check("replay: ส่ง strike ครบ", len(received_strikes) == 1)
    check("replay: ภาพเรดาร์ถูกเลื่อนเวลามาเป็นปัจจุบัน",
          all(abs((utcnow() - parse_iso(f.frame_ts)).total_seconds()) < 120
              for f in received_frames))
    check("replay: เรดาร์พร้อมใช้หลังเล่น", replay.radar_availability()[0], str(replay.radar_availability()))

    old = tmp / "capture_old.jsonl"
    old.write_text(json.dumps(strike) + "\n", encoding="utf-8")
    ok, reason = ReplaySource(file_name=str(old)).radar_availability()
    check("replay: ไฟล์รุ่นเก่าไม่มีเรดาร์ -> แจ้งชัดว่าไม่มี", not ok and "ไม่มีข้อมูลเรดาร์" in (reason or ""))


def main() -> None:
    test_palette_and_tiles()
    test_detect_cells()
    test_unmatched_colours()
    test_exposure()
    test_build_frame_and_fusion()
    test_replay_with_radar()
    for name in _passed:
        print(f"  ok  {name}")
    print(f"\nผ่านทั้งหมด {len(_passed)} ข้อ")


if __name__ == "__main__":
    main()
