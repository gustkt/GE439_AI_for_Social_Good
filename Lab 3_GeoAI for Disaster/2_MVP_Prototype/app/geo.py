"""ฟังก์ชันเรขาคณิตเชิงพื้นที่แบบเบา ๆ

MVP นี้เก็บแค่คอลัมน์ lat/lon ธรรมดาใน SQLite แล้วคำนวณระยะทางใน Python
ด้วยสูตร haversine - ตั้งใจ *ไม่* พึ่ง PostGIS/GEOS เพื่อให้ติดตั้งง่าย
ในระยะ < 100 กม. ความคลาดเคลื่อนของ haversine อยู่ระดับไม่กี่เมตร
ซึ่งละเอียดกว่าความแม่นยำของตำแหน่ง strike เองอยู่แล้ว
"""

from __future__ import annotations

import math

EARTH_RADIUS_KM = 6371.0088


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """ระยะทางวงกลมใหญ่ระหว่างสองพิกัด (กิโลเมตร)"""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    d_phi = p2 - p1
    d_lambda = math.radians(lon2 - lon1)
    a = (
        math.sin(d_phi / 2) ** 2
        + math.cos(p1) * math.cos(p2) * math.sin(d_lambda / 2) ** 2
    )
    return 2 * EARTH_RADIUS_KM * math.asin(math.sqrt(min(1.0, a)))


def to_local_xy(
    lat: float, lon: float, origin_lat: float, origin_lon: float
) -> tuple[float, float]:
    """แปลง lat/lon เป็นระนาบท้องถิ่น (x=ตะวันออก, y=เหนือ) หน่วยกิโลเมตร

    ใช้ equirectangular projection รอบจุด origin - แม่นพอในรัศมีไม่กี่สิบ กม.
    และทำให้ fit เวกเตอร์การเคลื่อนที่แบบเส้นตรงได้ตรงไปตรงมา
    """
    lat_km = math.pi * EARTH_RADIUS_KM / 180.0
    x = (lon - origin_lon) * lat_km * math.cos(math.radians(origin_lat))
    y = (lat - origin_lat) * lat_km
    return x, y


def from_local_xy(
    x: float, y: float, origin_lat: float, origin_lon: float
) -> tuple[float, float]:
    """ผกผันของ to_local_xy: ระนาบท้องถิ่น (กม.) -> lat/lon"""
    lat_km = math.pi * EARTH_RADIUS_KM / 180.0
    lat = origin_lat + y / lat_km
    lon = origin_lon + x / (lat_km * math.cos(math.radians(origin_lat)))
    return lat, lon


def offset_km(
    lat: float, lon: float, distance_km: float, bearing_deg: float
) -> tuple[float, float]:
    """หาพิกัดที่อยู่ห่างจาก (lat, lon) ไปตาม bearing (องศา, 0=เหนือ, ตามเข็ม)"""
    bearing = math.radians(bearing_deg)
    dx = distance_km * math.sin(bearing)  # ตะวันออก
    dy = distance_km * math.cos(bearing)  # เหนือ
    return from_local_xy(dx, dy, lat, lon)
