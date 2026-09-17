"""โหลดสนามจากไฟล์ GeoJSON เข้าตาราง `stadiums`

รันตรง ๆ ได้:  python -m app.seed_db
และ `app/main.py` ก็เรียกฟังก์ชันนี้ตอน startup ถ้าตารางยังว่าง

หมายเหตุเรื่องข้อมูล (สำคัญสำหรับรายงาน ห้ามกลบ):
  * Pattani มีสองสนาม - Tinsulanonda (สงขลา) ใช้จริงตอนนี้ = current
    ส่วน Rainbow Stadium (ปัตตานี) ยังปรับปรุงอยู่ = future
  * Bangkok United กับ BG Pathum United ใช้ True BG Stadium ร่วมกัน
    พิกัดจึงซ้ำกันโดยตั้งใจ (โหมด xweather ยุบเป็น cluster เดียวตอน poll)
  * รายการที่ note='verify' (Rasisalai United) พิกัดมาจากการ
    geocode ชื่อสนาม ยังไม่ได้ตรวจกับแหล่งอ้างอิง - ระบบจะติดธงไว้ให้เห็น
    และ *ไม่* อ้างว่าถูกต้อง
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from . import config, db

# พิกัดที่ยังไม่ได้ยืนยัน - แสดงเป็นคำเตือนทั้งบน log และบน UI
VERIFY_FLAG = "verify"


def load_features(geojson_path: Path) -> list[dict]:
    with geojson_path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError(f"{geojson_path} ไม่ใช่ FeatureCollection ที่ถูกต้อง")
    return features


def seed(geojson_path: Path | None = None) -> int:
    path = geojson_path or config.GEOJSON_PATH
    if not path.exists():
        raise FileNotFoundError(
            f"ไม่พบไฟล์ข้อมูลสนาม: {path}\n"
            "วางไฟล์ thai_league1_2026-27_stadiums.geojson ไว้ในโฟลเดอร์ data/"
        )

    db.init_db()
    inserted = 0
    needs_verify: list[str] = []

    for feature in load_features(path):
        geometry = feature.get("geometry") or {}
        coords = geometry.get("coordinates")
        properties = feature.get("properties") or {}

        if not (isinstance(coords, (list, tuple)) and len(coords) >= 2):
            print(f"  ข้าม (ไม่มีพิกัด): {properties.get('club')}", file=sys.stderr)
            continue

        lon, lat = float(coords[0]), float(coords[1])
        club = str(properties.get("club") or "").strip()
        name = str(properties.get("stadium") or "").strip()
        if not club or not name:
            print(f"  ข้าม (ไม่มีชื่อสโมสร/สนาม): {properties}", file=sys.stderr)
            continue

        capacity_raw = properties.get("capacity")
        capacity = int(capacity_raw) if isinstance(capacity_raw, (int, float)) else None
        status = str(properties.get("status") or "current").strip().lower()
        note = str(properties.get("note") or "").strip()

        db.upsert_stadium(club, name, lat, lon, capacity, status, note)
        inserted += 1

        if note == VERIFY_FLAG:
            needs_verify.append(f"{club} - {name} ({lat:.4f}, {lon:.4f})")

    print(f"seed สนามเรียบร้อย: {inserted} รายการ จาก {path.name}")
    if needs_verify:
        print("\n  ⚠ พิกัดต่อไปนี้ยังรอการตรวจสอบ (note=verify):")
        for line in needs_verify:
            print(f"      - {line}")
        print("    ที่มาเป็นการ geocode จากชื่อสนาม ควรยืนยันกับแหล่งอ้างอิง")
        print("    ก่อนนำตัวเลขระยะทางไปใช้อ้างอิงจริง\n")

    return inserted


if __name__ == "__main__":
    seed()
