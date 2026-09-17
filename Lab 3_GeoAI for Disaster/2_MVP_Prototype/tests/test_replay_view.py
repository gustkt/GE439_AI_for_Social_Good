"""ทดสอบโหมด replay บนหน้าเว็บ (คำนวณสถานะ ณ เวลาเสมือน) แบบ offline

รัน:  python -m tests.test_replay_view

ไฟล์ capture ในเทสต์นี้สร้างจาก tests/storm_fixture.py เพื่อให้รู้คำตอบล่วงหน้า
(พายุวิ่งเข้าหาสนามด้วยความเร็วที่รู้ค่า) - ใช้ทดสอบกลไก ไม่ใช่ข้อมูลที่ระบบแสดงจริง
เรียก API ผ่าน TestClient โดยไม่เปิด lifespan จึงไม่ต่อเครือข่ายใด ๆ
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="geoai_replay_view_"))
os.environ["DB_PATH"] = str(_TMP / "test.db")  # ต้องตั้งก่อน import app.config

from fastapi.testclient import TestClient  # noqa: E402

from app import config, db, seed_db  # noqa: E402
from app.main import app  # noqa: E402
from app.model.tiering import evaluate_tier  # noqa: E402
from app.timeutil import parse_iso, to_iso  # noqa: E402
from tests.storm_fixture import generate_storm_track  # noqa: E402

config.DB_PATH = _TMP / "test.db"
config.CAPTURE_DIR = _TMP / "captures"
config.CAPTURE_DIR.mkdir(parents=True, exist_ok=True)

_passed: list[str] = []
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
FILE = "capture_20260101T000000Z_fixture.jsonl"


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} ล้มเหลว {detail}")
    _passed.append(name)


def at(minutes: float) -> str:
    return to_iso(T0 + timedelta(minutes=minutes))


def status_of(payload: dict, stadium_id: int) -> dict:
    return next(r for r in payload["results"] if r["stadium_id"] == stadium_id)


def setup() -> tuple[dict, list]:
    seed_db.seed()
    stadium = db.list_stadiums(active_only=True)[0]
    strikes = generate_storm_track(stadium["lat"], stadium["lon"], T0, 0, 35)
    (config.CAPTURE_DIR / FILE).write_text(
        "\n".join(json.dumps(s.to_dict()) for s in strikes) + "\n", encoding="utf-8"
    )
    return stadium, strikes


def test_now_injection() -> None:
    strike = {"ts_iso": at(-5), "distance_km": 5.0}
    check("tiering: ใช้เวลาเสมือนได้ -> DANGER ณ เวลานั้น",
          evaluate_tier([strike], now=T0).status == "DANGER")
    check("tiering: เวลาปัจจุบันจริง strike เดียวกันเก่าไปแล้ว -> ALL_CLEAR",
          evaluate_tier([strike]).status == "ALL_CLEAR")


def test_api(client: TestClient, stadium: dict, strikes: list) -> None:
    listing = client.get("/api/replays").json()
    check("list: เจอไฟล์ replay", listing["count"] == 1 and listing["replays"][0]["file"] == FILE, str(listing))
    info = listing["replays"][0]
    check("list: นับ strike ครบ", info["strikes"] == len(strikes))
    check("list: สนามที่โดนหนักสุดคือสนามที่พายุวิ่งเข้าหา", info["peak"]["stadium_id"] == stadium["id"])
    check("list: ช่วงเวลาเริ่มจากเวลาในชื่อไฟล์", info["capture_start"] == to_iso(T0))

    before = client.get(f"/api/replays/{FILE}/risk", params={"t": at(0)}).json()
    check("risk: ก่อนพายุมา = ALL_CLEAR", status_of(before, stadium["id"])["status"] == "ALL_CLEAR")
    check("risk: มีข้อมูลฟ้าผ่าในช่วงที่บันทึก", before["lightning_available"])

    storm = client.get(f"/api/replays/{FILE}/risk", params={"t": at(27)}).json()
    row = status_of(storm, stadium["id"])
    check("risk: กลางพายุ = DANGER", row["status"] == "DANGER", row["status"])
    check("risk: นาฬิกา all-clear นับจากเวลาเสมือน", 0 < (row["all_clear_seconds_remaining"] or 0) <= 1800)
    check("risk: virtual_now ตรงกับที่ขอ", storm["virtual_now"] == at(27))

    last = max(parse_iso(s.ts_iso) for s in strikes)
    after_t = to_iso(last + timedelta(minutes=config.ALL_CLEAR_MINUTES + 1))
    after = client.get(f"/api/replays/{FILE}/risk", params={"t": after_t}).json()
    check("risk: พ้น 30 นาทีหลัง strike สุดท้าย = ALL_CLEAR",
          status_of(after, stadium["id"])["status"] == "ALL_CLEAR")

    early = client.get(f"/api/replays/{FILE}/strikes",
                       params={"stadium_id": stadium["id"], "t": at(10), "minutes": 30}).json()
    check("strikes: ส่งเฉพาะ strike ที่เกิดก่อนเวลาเสมือน",
          all(s["ts_iso"] <= at(10) for s in early["strikes"]) and early["count"] > 0)

    radar = client.get(f"/api/replays/{FILE}/radar", params={"t": at(27)}).json()
    check("radar: ไฟล์ไม่มีเรดาร์ -> แจ้งชัด ไม่มี tile ภาพสด",
          not radar["available"] and "เรดาร์" in radar["unavailable_reason"] and radar["tile_url_template"] is None)

    far = client.get(f"/api/replays/{FILE}/risk", params={"t": "2030-01-01T00:00:00Z"}).json()
    check("risk: เวลาเกินช่วงถูกบีบให้อยู่ในช่วงที่ดูได้", far["virtual_now"] == info["view_end"])

    check("security: ชื่อไฟล์แปลก ๆ อ่านไฟล์นอก captures ไม่ได้",
          client.get("/api/replays/..%2F..%2F.env/risk").status_code == 404)
    check("security: ไม่ใช่ .jsonl = 404", client.get("/api/replays/test.db/risk").status_code == 404)
    check("error: เวลาผิดรูปแบบ = 422",
          client.get(f"/api/replays/{FILE}/risk", params={"t": "เมื่อวาน"}).status_code == 422)


def main() -> None:
    stadium, strikes = setup()
    test_now_injection()
    # ไม่ใช้ `with TestClient(app)` เพื่อไม่ให้ lifespan เปิดตัวรับข้อมูลสด/เรดาร์
    test_api(TestClient(app), stadium, strikes)
    for name in _passed:
        print(f"  ok  {name}")
    print(f"\nผ่านทั้งหมด {len(_passed)} ข้อ")


if __name__ == "__main__":
    main()
