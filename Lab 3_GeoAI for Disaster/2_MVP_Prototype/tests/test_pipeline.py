"""ทดสอบเส้นทาง ingest -> SQLite -> tiering และ capture -> replay แบบ offline

รัน:  python -m tests.test_pipeline

ใช้ฐานข้อมูลและโฟลเดอร์ capture ชั่วคราว ไม่แตะ data/ ของโปรเจกต์

หมายเหตุ: ไฟล์ capture ในเทสต์นี้สร้างจาก tests/storm_fixture.py เพื่อทดสอบ *กลไก*
การ replay เท่านั้น ไม่ใช่ข้อมูลฟ้าผ่าจริง และถูกลบทิ้งพร้อมโฟลเดอร์ชั่วคราว
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
from datetime import timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="geoai_test_"))
os.environ["DB_PATH"] = str(_TMP / "test.db")  # ต้องตั้งก่อน import app.config

from app import config, db, seed_db  # noqa: E402
from app.geo import haversine_km, offset_km  # noqa: E402
from app.ingest import Ingestor  # noqa: E402
from app.model.tiering import evaluate_tier  # noqa: E402
from app.sources.base import Strike  # noqa: E402
from app.sources.replay import ReplaySource  # noqa: E402
from tests.storm_fixture import generate_storm_track  # noqa: E402
from app.timeutil import minutes_since, now_iso, to_iso, utcnow  # noqa: E402

config.DB_PATH = _TMP / "test.db"
config.CAPTURE_DIR = _TMP / "captures"

_passed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} ล้มเหลว {detail}")
    _passed.append(name)


def setup() -> dict:
    seed_db.seed()
    return db.list_stadiums(active_only=True)[0]


def test_ingest_filter(stadium: dict) -> None:
    db.clear_strikes()
    ingest = Ingestor(capture=False)
    near_lat, near_lon = offset_km(stadium["lat"], stadium["lon"], 5.0, 90.0)
    near = Strike(ts_iso=now_iso(), lat=near_lat, lon=near_lon, source="blitzortung")
    far = Strike(ts_iso=now_iso(), lat=48.85, lon=2.35, source="blitzortung")  # ปารีส

    ingest([near, far])
    rows = db.recent_strikes(stadium["id"], 60)
    check("ingest: เก็บ strike ใกล้สนาม", len(rows) == 1, f"ได้ {len(rows)} แถว")
    check("ingest: ทิ้ง strike จากอีกซีกโลก", ingest.received_total == 2 and db.count_strikes() == 1)
    check("ingest: ระยะคำนวณด้วย haversine", abs(rows[0]["distance_km"] - 5.0) < 0.05, str(rows[0]["distance_km"]))
    check("ingest: บันทึก source", rows[0]["source"] == "blitzortung")

    ingest([near])
    check("ingest: strike ซ้ำไม่ถูกเก็บซ้ำ", len(db.recent_strikes(stadium["id"], 60)) == 1)


def test_provider_distance(stadium: dict) -> None:
    db.clear_strikes()
    ingest = Ingestor(capture=False)
    lat, lon = offset_km(stadium["lat"], stadium["lon"], 5.0, 0.0)
    at_stadium = Strike(
        ts_iso=now_iso(), lat=lat, lon=lon, source="xweather",
        ref_lat=stadium["lat"], ref_lon=stadium["lon"], ref_distance_km=5.123,
    )
    ingest([at_stadium])
    row = db.recent_strikes(stadium["id"], 60)[0]
    check("ingest: ใช้ relativeTo.distanceKM เมื่อจุด query คือสนาม", row["distance_km"] == 5.123, str(row))

    db.clear_strikes()
    elsewhere = Strike(
        ts_iso=to_iso(utcnow() - timedelta(seconds=1)), lat=lat, lon=lon, source="xweather",
        ref_lat=stadium["lat"] + 0.05, ref_lon=stadium["lon"], ref_distance_km=99.0,
    )
    ingest([elsewhere])
    row = db.recent_strikes(stadium["id"], 60)[0]
    expected = haversine_km(stadium["lat"], stadium["lon"], lat, lon)
    check("ingest: จุด query ไม่ใช่สนาม -> คำนวณระยะเอง", abs(row["distance_km"] - expected) < 0.01, str(row))


def test_capture(stadium: dict) -> None:
    db.clear_strikes()
    ingest = Ingestor(capture=True)
    lat, lon = offset_km(stadium["lat"], stadium["lon"], 8.0, 180.0)
    ingest([
        Strike(ts_iso=now_iso(), lat=lat, lon=lon, source="blitzortung", polarity=-1, station_count=11),
        Strike(ts_iso=now_iso(), lat=-33.9, lon=151.2, source="blitzortung"),  # ซิดนีย์
    ])
    assert ingest.capture_path is not None
    lines = ingest.capture_path.read_text(encoding="utf-8").splitlines()
    check("capture: เขียนเฉพาะ strike ใกล้สนามลง JSONL", len(lines) == 1, f"ได้ {len(lines)} บรรทัด")
    record = json.loads(lines[0])
    check("capture: เก็บฟิลด์ครบ", record["source"] == "blitzortung" and record["station_count"] == 11)


def test_replay(stadium: dict) -> None:
    # fixture สังเคราะห์: พายุผ่านสนามเมื่อวานนี้ ช่วง 15 นาที
    t0 = utcnow() - timedelta(days=1)
    strikes = generate_storm_track(stadium["lat"], stadium["lon"], t0, 10, 25)
    capture = _TMP / "captures" / "fixture_synthetic.jsonl"
    capture.parent.mkdir(parents=True, exist_ok=True)
    capture.write_text("\n".join(json.dumps(s.to_dict()) for s in strikes) + "\nบรรทัดเสีย\n", encoding="utf-8")

    replay = ReplaySource(file_name=capture.name, speed=600)  # 1 วินาที = 10 นาทีของต้นฉบับ
    check("replay: อ่านไฟล์และข้ามบรรทัดเสีย", len(replay.records) == len(strikes), f"{len(replay.records)}/{len(strikes)}")
    check("replay: label บอกว่าเป็น REPLAY พร้อมวันที่", replay.label.startswith("REPLAY ข้อมูลจริง 20"), replay.label)

    db.clear_strikes()
    ingest = Ingestor(capture=False)
    asyncio.run(asyncio.wait_for(replay.run(ingest), timeout=15))
    check("replay: เล่นครบทุก strike", replay.finished and replay.emitted == len(strikes), str(replay.status()))

    rows = db.recent_strikes(stadium["id"], 60)
    check("replay: strike ถูกเลื่อนเวลามาเป็นปัจจุบัน", rows and all((minutes_since(r["ts_iso"]) or 99) < 1 for r in rows))
    check("replay: ติดป้าย source ว่ามาจากไฟล์ replay", rows and rows[0]["source"] == "replay:fixture_synthetic.jsonl")
    tier = evaluate_tier(rows)
    check("replay: pipeline คิดสถานะเตือนจากข้อมูลที่เล่นซ้ำ", tier.status in {"SUSPEND", "DANGER"}, tier.status)


def main() -> None:
    stadium = setup()
    test_ingest_filter(stadium)
    test_provider_distance(stadium)
    test_capture(stadium)
    test_replay(stadium)
    for name in _passed:
        print(f"  ok  {name}")
    print(f"\nผ่านทั้งหมด {len(_passed)} ข้อ")


if __name__ == "__main__":
    main()
