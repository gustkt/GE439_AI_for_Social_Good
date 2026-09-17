"""บันทึกผลทดสอบรับข้อมูลสดจาก Blitzortung ของเซิร์ฟเวอร์ที่รันอยู่ ลง docs/figures/live_blitzortung.json

รัน:  python -m docs.build.snapshot_live      (ต้องเปิดเซิร์ฟเวอร์ที่ http://127.0.0.1:8000 ไว้ก่อน)

ช่วงเวลาทดสอบนับจากเวลาเริ่มไฟล์ capture ของเซิร์ฟเวอร์ (ชื่อไฟล์ capture_<เวลาเริ่ม>.jsonl) ถึงตอนนี้
"""

from __future__ import annotations

import json
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SERVER = "http://127.0.0.1:8000"
THAI = timedelta(hours=7)


def main() -> None:
    meta = json.load(urllib.request.urlopen(f"{SERVER}/api/meta", timeout=10))
    risk = json.load(urllib.request.urlopen(f"{SERVER}/api/risk", timeout=30))
    ingest = meta["ingest"]
    started = datetime.strptime(ingest["capture_file"][8:24], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    minutes = max(1.0, (now - started).total_seconds() / 60)
    snapshot = {
        "window": f"{(started + THAI):%d/%m/%Y %H:%M}–{(now + THAI):%H:%M} น. ({minutes:.0f} นาที)",
        "minutes": round(minutes, 1),
        "received_total": ingest["received_total"],
        "per_minute": ingest["received_total"] / minutes,
        "in_region_total": ingest["in_region_total"],
        "nearest_km": ingest["nearest_km_since_start"],
        "nearest_stadium": ingest["nearest_stadium"],
        "stored_total": ingest["stored_total"],
        "sources": {name: s.get("available") for name, s in meta["source_status"].get("sources", {}).items()},
        "summary": risk["summary"],
    }
    target = ROOT / "docs" / "figures" / "live_blitzortung.json"
    target.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(snapshot, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
