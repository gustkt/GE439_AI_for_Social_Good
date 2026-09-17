"""GeoAI จากภาพเรดาร์: สี -> dBZ -> hotspot -> cell -> ระยะถึงสนาม

ขั้นตอน (ทุกขั้นเป็นฟังก์ชันล้วน ทดสอบได้โดยไม่ต้องต่อเครือข่าย):
  1. แปลงสีแต่ละ pixel เป็นค่าการสะท้อน (dBZ) ด้วยตารางสีทางการของ RainViewer
     (palette "Universal Blue" ซึ่งเป็น palette เดียวที่ free tier ให้ใช้)
     ต้องขอภาพแบบปิด smoothing ไม่งั้นสีขอบจะถูกผสมจนเทียบตารางไม่ได้
  2. hotspot = pixel ที่ dBZ >= เกณฑ์ (default 40 dBZ) ค่านี้ใช้กันทั่วไปเป็นตัวแทน
     ของฝนฟ้าคะนองแบบ convective เป็นค่าประมาณ ไม่ได้การันตีว่ามีฟ้าผ่า
  3. จัดกลุ่ม hotspot ที่ติดกัน (8 ทิศ) เป็น "cell" แล้วตัด cell เล็กที่เป็นสัญญาณรบกวน
  4. ต่อสนามหนึ่งแห่ง: ระยะถึง hotspot ที่ใกล้ที่สุด, dBZ สูงสุดในวง WATCH
     และจุดศูนย์ถ่วงของ hotspot รอบสนาม (ใช้ทำ nowcast การเคลื่อนที่)

ภาพเรดาร์เป็นค่าสะท้อนรวมทุกความสูง จึงบอกได้ว่า "มีฝนแรง" แต่บอกไม่ได้ว่า
"มีประจุไฟฟ้า" ระบบจึงใช้ผลนี้เป็นสัญญาณเตือนล่วงหน้า (WATCH) เท่านั้น
"""

from __future__ import annotations

import csv
import io
import math
from collections import deque
from collections.abc import Iterable
from dataclasses import asdict, dataclass

from ..geo import haversine_km

TILE_SIZE = 256
EARTH_CIRCUMFERENCE_KM = 40075.016686
_NO_ECHO = -127  # ค่าแทน pixel ที่ไม่มีสัญญาณเรดาร์ (โปร่งใส)


# ------------------------------------------------------------------ palette


def load_palette(csv_text: str, scheme: str = "Universal Blue") -> dict[tuple[int, int, int, int], int]:
    """ตารางสีทางการ (CSV) -> dict จากสี RGBA ไปเป็นค่า dBZ"""
    palette: dict[tuple[int, int, int, int], int] = {}
    for row in csv.DictReader(io.StringIO(csv_text)):
        hex_colour = (row.get(scheme) or "").strip().lstrip("#")
        if len(hex_colour) != 8 or hex_colour.endswith("00"):
            continue  # สีโปร่งใส = ไม่มีสัญญาณ
        rgba = tuple(int(hex_colour[i : i + 2], 16) for i in (0, 2, 4, 6))
        palette.setdefault(rgba, int(row["dBZ / RGBA"]))
    if not palette:
        raise ValueError(f"ไม่พบ palette '{scheme}' ในตารางสี")
    return palette


# --------------------------------------------------------------- tile math
# ภาพเรดาร์เป็น tile แบบ Web Mercator เหมือนแผนที่ทั่วไป พิกัด "pixel ระดับโลก"
# ที่ zoom z คือพิกัดบนภาพโลกขนาด 256 x 2^z pixel ต่อด้าน


def lonlat_to_pixel(lat: float, lon: float, zoom: int) -> tuple[float, float]:
    size = TILE_SIZE * 2**zoom
    lat_rad = math.radians(max(min(lat, 85.0511), -85.0511))
    x = (lon + 180.0) / 360.0 * size
    y = (1.0 - math.log(math.tan(lat_rad) + 1.0 / math.cos(lat_rad)) / math.pi) / 2.0 * size
    return x, y


def pixel_to_lonlat(x: float, y: float, zoom: int) -> tuple[float, float]:
    size = TILE_SIZE * 2**zoom
    lon = x / size * 360.0 - 180.0
    lat = math.degrees(math.atan(math.sinh(math.pi * (1.0 - 2.0 * y / size))))
    return lat, lon


def pixel_size_km(lat: float, zoom: int) -> float:
    """ความกว้างของ 1 pixel บนพื้นโลก (กม.) - ที่ zoom 7 ละติจูดไทยได้ราว 1.2 กม."""
    return EARTH_CIRCUMFERENCE_KM * math.cos(math.radians(lat)) / (TILE_SIZE * 2**zoom)


def tiles_covering(points: Iterable[tuple[float, float]], radius_km: float, zoom: int) -> set[tuple[int, int]]:
    """tile ทั้งหมดที่ต้องโหลดเพื่อให้ครอบคลุมรัศมีรอบทุกจุด"""
    tiles: set[tuple[int, int]] = set()
    for lat, lon in points:
        px, py = lonlat_to_pixel(lat, lon, zoom)
        reach = radius_km / pixel_size_km(lat, zoom)
        for tx in range(int((px - reach) // TILE_SIZE), int((px + reach) // TILE_SIZE) + 1):
            for ty in range(int((py - reach) // TILE_SIZE), int((py + reach) // TILE_SIZE) + 1):
                tiles.add((tx, ty))
    return tiles


# ---------------------------------------------------------------- dBZ grid


class DbzGrid:
    """ภาพเรดาร์หลาย tile ต่อกันเป็นตาราง dBZ เดียว อ้างอิงด้วยพิกัด pixel ระดับโลก"""

    def __init__(self, zoom: int) -> None:
        self.zoom = zoom
        self._tiles: dict[tuple[int, int], list[int]] = {}
        self.unmatched_pixels = 0

    @property
    def tile_count(self) -> int:
        return len(self._tiles)

    def add_tile(self, tx: int, ty: int, rgba_pixels: Iterable[tuple[int, int, int, int]],
                 palette: dict[tuple[int, int, int, int], int]) -> None:
        values: list[int] = []
        for pixel in rgba_pixels:
            if pixel[3] == 0:
                values.append(_NO_ECHO)
                continue
            dbz = palette.get(tuple(pixel))
            if dbz is None:
                # สีไม่ตรงตาราง (เช่นลืมปิด smoothing) - นับไว้ตรวจคุณภาพข้อมูล ไม่เดาค่า
                self.unmatched_pixels += 1
                values.append(_NO_ECHO)
            else:
                values.append(dbz)
        if len(values) != TILE_SIZE * TILE_SIZE:
            raise ValueError(f"tile {tx},{ty} มี {len(values)} pixel ไม่ใช่ {TILE_SIZE * TILE_SIZE}")
        self._tiles[(tx, ty)] = values

    def dbz(self, gx: int, gy: int) -> int | None:
        tile = self._tiles.get((gx // TILE_SIZE, gy // TILE_SIZE))
        if tile is None:
            return None
        value = tile[(gy % TILE_SIZE) * TILE_SIZE + (gx % TILE_SIZE)]
        return None if value == _NO_ECHO else value

    def max_dbz(self) -> int | None:
        best = max((v for tile in self._tiles.values() for v in tile), default=_NO_ECHO)
        return None if best == _NO_ECHO else best

    def hotspots(self, threshold: int) -> dict[tuple[int, int], int]:
        """pixel ที่ dBZ >= threshold -> {(gx, gy): dBZ}"""
        found: dict[tuple[int, int], int] = {}
        for (tx, ty), tile in self._tiles.items():
            for index, value in enumerate(tile):
                if value >= threshold:
                    found[(tx * TILE_SIZE + index % TILE_SIZE, ty * TILE_SIZE + index // TILE_SIZE)] = value
        return found


# ------------------------------------------------------------------- cells


@dataclass
class RadarCell:
    lat: float
    lon: float
    max_dbz: int
    pixels: int
    area_km2: float

    def to_dict(self) -> dict:
        return asdict(self)


def detect_cells(grid: DbzGrid, threshold: int, min_pixels: int) -> list[RadarCell]:
    """จัดกลุ่ม hotspot ที่ติดกัน (8 ทิศ) เป็น cell - ใช้ BFS ธรรมดา ไม่พึ่ง scipy"""
    hotspots = grid.hotspots(threshold)
    visited: set[tuple[int, int]] = set()
    cells: list[RadarCell] = []

    for start in hotspots:
        if start in visited:
            continue
        queue = deque([start])
        visited.add(start)
        members: list[tuple[int, int]] = []
        while queue:
            gx, gy = queue.popleft()
            members.append((gx, gy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    neighbour = (gx + dx, gy + dy)
                    if neighbour in hotspots and neighbour not in visited:
                        visited.add(neighbour)
                        queue.append(neighbour)

        if len(members) < min_pixels:
            continue  # กลุ่มเล็กเกินไป มักเป็นสัญญาณรบกวน/clutter
        cx = sum(m[0] for m in members) / len(members) + 0.5
        cy = sum(m[1] for m in members) / len(members) + 0.5
        lat, lon = pixel_to_lonlat(cx, cy, grid.zoom)
        cells.append(
            RadarCell(
                lat=round(lat, 5),
                lon=round(lon, 5),
                max_dbz=max(hotspots[m] for m in members),
                pixels=len(members),
                area_km2=round(len(members) * pixel_size_km(lat, grid.zoom) ** 2, 1),
            )
        )

    cells.sort(key=lambda c: (-c.max_dbz, -c.pixels))
    return cells


# ---------------------------------------------------------------- exposure


@dataclass
class StadiumExposure:
    stadium_id: int
    nearest_hotspot_km: float | None   # ระยะถึง pixel >= เกณฑ์ที่ใกล้ที่สุด (ในรัศมีค้นหา)
    max_dbz_watch: int | None          # dBZ สูงสุดภายในวง WATCH
    activity_lat: float | None         # จุดศูนย์ถ่วงของ hotspot ในรัศมีค้นหา (ใช้ nowcast)
    activity_lon: float | None
    activity_pixels: int

    def to_dict(self) -> dict:
        return asdict(self)


def stadium_exposure(
    grid: DbzGrid,
    stadium_id: int,
    lat: float,
    lon: float,
    threshold: int,
    watch_km: float,
    search_km: float,
) -> StadiumExposure:
    """วัดว่าฝนแรงอยู่ใกล้สนามแค่ไหน โดยไล่ pixel ในกรอบรอบสนามเท่านั้น (เร็ว)"""
    px, py = lonlat_to_pixel(lat, lon, grid.zoom)
    reach = int(math.ceil(search_km / pixel_size_km(lat, grid.zoom))) + 1

    nearest: float | None = None
    max_watch: int | None = None
    sum_lat = sum_lon = 0.0
    count = 0
    for gx in range(int(px) - reach, int(px) + reach + 1):
        for gy in range(int(py) - reach, int(py) + reach + 1):
            value = grid.dbz(gx, gy)
            if value is None:
                continue
            plat, plon = pixel_to_lonlat(gx + 0.5, gy + 0.5, grid.zoom)
            distance = haversine_km(lat, lon, plat, plon)
            if distance > search_km:
                continue
            if distance <= watch_km and (max_watch is None or value > max_watch):
                max_watch = value
            if value >= threshold:
                if nearest is None or distance < nearest:
                    nearest = distance
                sum_lat += plat
                sum_lon += plon
                count += 1

    return StadiumExposure(
        stadium_id=stadium_id,
        nearest_hotspot_km=round(nearest, 2) if nearest is not None else None,
        max_dbz_watch=max_watch,
        activity_lat=round(sum_lat / count, 5) if count else None,
        activity_lon=round(sum_lon / count, 5) if count else None,
        activity_pixels=count,
    )
