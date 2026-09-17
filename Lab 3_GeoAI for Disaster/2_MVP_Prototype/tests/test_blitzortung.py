"""ทดสอบตัว decode ของ Blitzortung แบบ offline (ไม่ต่อเครือข่าย)

รัน:  python -m tests.test_blitzortung

วิธีทดสอบ: เขียนตัวบีบอัด LZW แบบมาตรฐานขึ้นมา บีบ JSON ตัวอย่าง แล้วให้
`lzw_decode` คลายกลับ ถ้าได้ข้อความเดิมทุกตัวอักษร แปลว่า decoder ถูกต้อง
รวมถึงกรณีพิเศษ cScSc ที่เกิดกับข้อความซ้ำ ๆ อย่าง "aaaaaaa"
"""

from __future__ import annotations

import json

from app.sources.blitzortung import lzw_decode, parse_frame

_passed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} ล้มเหลว {detail}")
    _passed.append(name)


def lzw_encode(text: str) -> str:
    """ตัวบีบอัด LZW มาตรฐาน (ตัวอักษรเดี่ยว = ตัวมันเอง, วลีใหม่เริ่มที่รหัส 256)"""
    phrases: dict[str, int] = {}
    next_code = 256
    current = text[0]
    output: list[str] = []
    for char in text[1:]:
        candidate = current + char
        if candidate in phrases:
            current = candidate
            continue
        output.append(current if len(current) == 1 else chr(phrases[current]))
        phrases[candidate] = next_code
        next_code += 1
        current = char
    output.append(current if len(current) == 1 else chr(phrases[current]))
    return "".join(output)


SAMPLE = {
    "time": 1789385902785000000,  # 2026-09-14T11:38:22.785Z ในหน่วย nanosecond
    "lat": 7.2071,
    "lon": 100.5986,
    "alt": 0,
    "pol": -1,
    "mds": 8421,
    "mcg": 212,
    "status": 1,
    "region": 7,
    "sig": [{"sta": 1234, "time": 1, "lat": 13.7, "lon": 100.5, "alt": 0, "status": 1}] * 9,
    "delay": 3.1,
    "lonc": 0,
    "latc": 0,
}


def test_roundtrip() -> None:
    for text in ["a", "ab", "aaaaaaaaaaaa", "abababababab", "TOBEORNOTTOBEORTOBEORNOT", json.dumps(SAMPLE)]:
        encoded = lzw_encode(text)
        check(f"lzw: คลายกลับได้ตรง ({text[:16]!r})", lzw_decode(encoded) == text)
    check("lzw: ข้อความยาวถูกบีบให้สั้นลงจริง", len(lzw_encode(json.dumps(SAMPLE))) < len(json.dumps(SAMPLE)))
    check("lzw: ข้อความว่าง", lzw_decode("") == "")


def test_parse_frame() -> None:
    strike = parse_frame(lzw_encode(json.dumps(SAMPLE)))
    check("parse: decode ได้ Strike", strike is not None)
    assert strike is not None
    check("parse: เวลาแปลงจาก nanosecond ถูกต้อง", strike.ts_iso == "2026-09-14T11:38:22Z", strike.ts_iso)
    check("parse: พิกัดถูกต้อง", (strike.lat, strike.lon) == (7.2071, 100.5986))
    check("parse: ขั้วประจุ", strike.polarity == -1)
    check("parse: นับจำนวนสถานีจาก sig", strike.station_count == 9)
    check("parse: ติดป้าย source", strike.source == "blitzortung")

    check("parse: frame เสีย -> None", parse_frame(lzw_encode('{"not":"json"')) is None)
    check("parse: ไม่มีพิกัด -> None", parse_frame(lzw_encode('{"time": 1}')) is None)
    bad = dict(SAMPLE, lat=999)
    check("parse: พิกัดเกินขอบเขต -> None", parse_frame(lzw_encode(json.dumps(bad))) is None)


def main() -> None:
    test_roundtrip()
    test_parse_frame()
    for name in _passed:
        print(f"  ok  {name}")
    print(f"\nผ่านทั้งหมด {len(_passed)} ข้อ")


if __name__ == "__main__":
    main()
