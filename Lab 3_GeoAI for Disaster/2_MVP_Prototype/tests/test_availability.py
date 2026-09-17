"""ทดสอบว่าระบบไม่ตอบ "ปลอดภัย" เมื่อไม่มีข้อมูล และจัดการ quota หมดได้ถูกต้อง

รัน:  python -m tests.test_availability

เคสจริงที่เทสต์นี้กันไว้ (2026-09-16): quota ของ Xweather หมด API ตอบ
HTTP 429 + code "maxhits" แต่โค้ดเดิมเหมาว่าเป็น rate limit ชั่วคราว ยิงซ้ำทุก 4 วินาที
และหน้าเว็บขึ้นทุกสนามเป็น ALL_CLEAR ทั้งที่ไม่มีข้อมูลเลย
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta

import httpx

from app import config
from app.model.fusion import fuse
from app.sources.base import LightningSource, Strike
from app.sources.blitzortung import BlitzortungSource
from app.sources.merged import MergedSource, StrikeDeduplicator
from app.sources.xweather import (
    CredentialRejected,
    QuotaExhausted,
    RateLimited,
    XweatherSource,
)
from app.timeutil import utcnow

_passed: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if not condition:
        raise AssertionError(f"{name} ล้มเหลว {detail}")
    _passed.append(name)


# ------------------------------------------------------------------ fusion


def test_fusion() -> None:
    check("fusion: มีข้อมูลและไม่มี strike = ALL_CLEAR",
          fuse("ALL_CLEAR", True).status == "ALL_CLEAR")

    no_data = fuse("ALL_CLEAR", False, "quota หมด")
    check("fusion: ไม่มีข้อมูลฟ้าผ่า = NO_DATA ไม่ใช่ ALL_CLEAR", no_data.status == "NO_DATA", no_data.status)
    check("fusion: NO_DATA บอกเหตุผล", "quota หมด" in " ".join(no_data.reasons), str(no_data.reasons))

    check("fusion: quota หมดกลางพายุ strike เดิมยังคง SUSPEND",
          fuse("SUSPEND", False, "quota หมด").status == "SUSPEND")
    check("fusion: quota หมดกลางพายุ strike เดิมยังคง DANGER",
          fuse("DANGER", False, "quota หมด").status == "DANGER")
    check("fusion: strike ในวง WATCH ยังเป็น WATCH แม้ข้อมูลใหม่ไม่มา",
          fuse("WATCH", False, "quota หมด").status == "WATCH")

    check("fusion: เรดาร์พบ cell แรง -> WATCH แม้ไม่มี strike",
          fuse("ALL_CLEAR", True, radar_watch=True).status == "WATCH")
    check("fusion: เรดาร์พบ cell แรงขณะไม่มีข้อมูลฟ้าผ่า -> WATCH",
          fuse("ALL_CLEAR", False, "quota หมด", radar_watch=True).status == "WATCH")
    check("fusion: เรดาร์อย่างเดียวไม่ทำให้ขึ้น SUSPEND",
          fuse("ALL_CLEAR", True, radar_watch=True).status not in ("SUSPEND", "DANGER"))


# -------------------------------------------------------------- Xweather


def _source_with(handler) -> tuple[XweatherSource, httpx.AsyncClient]:
    config.XWEATHER_CLIENT_ID = config.XWEATHER_CLIENT_ID or "test-id"
    config.XWEATHER_CLIENT_SECRET = config.XWEATHER_CLIENT_SECRET or "test-secret"
    return XweatherSource(), httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _fetch_error(status: int, body: dict) -> Exception | None:
    source, http = _source_with(lambda request: httpx.Response(status, json=body))

    async def go():
        async with http:
            try:
                await source.fetch(http, 13.7, 100.5, 30)
            except Exception as exc:  # noqa: BLE001
                return exc
            return None

    return asyncio.run(go())


def test_xweather_errors() -> None:
    maxhits = {"success": False, "error": {"code": "maxhits",
               "description": "Maximum number of accesses reached for subscription period."}}
    check("xweather: 429 + maxhits = QuotaExhausted (ถาวร)",
          isinstance(_fetch_error(429, maxhits), QuotaExhausted))
    check("xweather: 429 แบบอื่น = RateLimited (ชั่วคราว)",
          isinstance(_fetch_error(429, {"success": False, "error": {"code": "too_many"}}), RateLimited))
    check("xweather: 401 = CredentialRejected",
          isinstance(_fetch_error(401, {"success": False, "error": {"code": "invalid_client"}}),
                     CredentialRejected))
    check("xweather: ไม่มีข้อมูลในพื้นที่ (warn_) ไม่ใช่ error",
          _fetch_error(200, {"success": False, "error": {"code": "warn_no_data"}}) is None)


def test_xweather_parses_success() -> None:
    body = {"type": "FeatureCollection", "features": [{
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [100.7563, 6.734]},
        "properties": {
            "loc": {"long": 100.7563, "lat": 6.734},
            "ob": {"dateTimeISO": "2026-09-14T11:38:22+00:00",
                   "pulse": {"type": "cg", "peakamp": -7000, "numSensors": 7}},
            "relativeTo": {"distanceKM": 54.613},
        },
    }]}
    source, http = _source_with(lambda request: httpx.Response(200, json=body))

    async def go():
        async with http:
            return await source.fetch(http, 7.2, 100.6, 30)

    strikes = asyncio.run(go())
    check("xweather: แปลง record จริงได้", len(strikes) == 1 and strikes[0].ts_iso == "2026-09-14T11:38:22Z")
    check("xweather: เก็บระยะจาก API", strikes[0].ref_distance_km == 54.613)


def test_xweather_availability() -> None:
    source, _ = _source_with(lambda request: httpx.Response(200, json={}))
    ok, reason = source.availability()
    check("availability: ก่อน poll สำเร็จครั้งแรก = ไม่พร้อม", not ok and bool(reason), str(reason))

    source._mark_success()
    check("availability: เพิ่ง poll สำเร็จ = พร้อม", source.availability()[0])

    source.last_success = utcnow() - timedelta(minutes=30)
    ok, reason = source.availability()
    check("availability: ไม่ได้ข้อมูลใหม่นานเกิน = ไม่พร้อม", not ok and "นาที" in (reason or ""), str(reason))

    source._mark_success()
    source._mark_unavailable("quota ของ Xweather หมดแล้ว")
    ok, reason = source.availability()
    check("availability: quota หมด = ไม่พร้อม แม้เพิ่ง poll สำเร็จ", not ok and "quota" in (reason or ""))

    source._mark_success()
    check("availability: quota กลับมา = พร้อมอีกครั้ง", source.availability()[0])


# ------------------------------------------------- เครือข่ายที่จับได้ไม่ครบ (Blitzortung)


def test_sparse_fusion() -> None:
    clear = fuse("ALL_CLEAR", True, radar_available=True, lightning_sparse=True)
    check("sparse: ไม่มีฟ้าผ่า + เรดาร์ไม่พบฝนแรง = ALL_CLEAR", clear.status == "ALL_CLEAR", clear.status)
    no_radar = fuse("ALL_CLEAR", True, radar_available=False, radar_unavailable_reason="เรดาร์ล่ม",
                    lightning_sparse=True)
    check("sparse: ไม่มีฟ้าผ่า + ไม่มีเรดาร์ = NO_DATA (ห้ามบอกว่าปลอดภัย)",
          no_radar.status == "NO_DATA", no_radar.status)
    check("sparse: NO_DATA บอกว่าเรดาร์ไม่พร้อม", "เรดาร์ล่ม" in " ".join(no_radar.reasons))
    check("เครือข่ายครบ: ไม่มีฟ้าผ่า + ไม่มีเรดาร์ ยังเป็น ALL_CLEAR ได้ (พฤติกรรมเดิม)",
          fuse("ALL_CLEAR", True, radar_available=False).status == "ALL_CLEAR")
    watch = fuse("ALL_CLEAR", True, radar_watch=True, lightning_sparse=True)
    check("sparse: เรดาร์พบฝนแรง = WATCH และเตือนว่าเครือข่ายอาจตรวจไม่ครบ",
          watch.status == "WATCH" and any("ไม่ครบ" in r for r in watch.reasons), str(watch.reasons))
    check("sparse: strike ยืนยันในวง 13 กม. ยังสั่งหยุดได้",
          fuse("SUSPEND", True, radar_available=False, lightning_sparse=True).status == "SUSPEND")


def test_blitzortung_availability() -> None:
    source = BlitzortungSource(hosts=["example.invalid"])
    check("blitzortung: ยังไม่เชื่อมต่อ = ไม่พร้อม", not source.availability()[0])
    check("blitzortung: ถือว่าจับได้ไม่ครบในไทย", source.coverage_sparse())
    source.connected_host = "example.invalid"
    source._connected_mono = time.monotonic()
    check("blitzortung: เพิ่งเชื่อมต่อ = พร้อม", source.availability()[0])
    source._last_frame_mono = time.monotonic() - config.BLITZORTUNG_STALE_SECONDS - 5
    ok, reason = source.availability()
    check("blitzortung: ต่ออยู่แต่ไม่มี frame นานเกิน = ไม่พร้อม (stream ค้าง)",
          not ok and "ค้าง" in (reason or ""), str(reason))


class _FakeSource(LightningSource):
    def __init__(self, name: str, available: bool, sparse: bool, ids: set[int] | None = None) -> None:
        self.name, self.label = name, name.upper()
        self._available, self.sparse_coverage, self._ids = available, sparse, ids

    async def run(self, on_strikes) -> None:
        return None

    def availability(self):
        return (True, None) if self._available else (False, f"{self.name} ไม่พร้อม")

    def monitored_stadium_ids(self):
        return self._ids


def test_merged_source() -> None:
    free = _FakeSource("blitzortung", True, True)
    paid_down = _FakeSource("xweather", False, False)
    merged = MergedSource([free, paid_down])
    check("merged: มีแหล่งหนึ่งพร้อม = พร้อม", merged.availability()[0])
    check("merged: quota Xweather หมด เหลือ Blitzortung = จับได้ไม่ครบ", merged.coverage_sparse())

    paid_down._available = True
    check("merged: Xweather กลับมา = ไม่ sparse", not merged.coverage_sparse())

    both_down = MergedSource([_FakeSource("a", False, True), _FakeSource("b", False, False)])
    ok, reason = both_down.availability()
    check("merged: ทุกแหล่งไม่พร้อม = ไม่พร้อม พร้อมเหตุผลของทุกแหล่ง",
          not ok and "a ไม่พร้อม" in reason and "b ไม่พร้อม" in reason, str(reason))

    subset = MergedSource([_FakeSource("a", True, False, {2}), _FakeSource("b", True, True, None)])
    check("merged: มีแหล่งที่เฝ้าทุกสนาม = เฝ้าทุกสนาม", subset.monitored_stadium_ids() is None)

    dedupe = StrikeDeduplicator(seconds=1.0, km=5.0)
    first = Strike("2026-09-16T06:30:00Z", 11.80, 99.79, "blitzortung")
    same_other_net = Strike("2026-09-16T06:30:01Z", 11.82, 99.80, "xweather")
    far = Strike("2026-09-16T06:30:00Z", 12.30, 99.79, "xweather")
    later = Strike("2026-09-16T06:30:05Z", 11.80, 99.79, "xweather")
    same_net = Strike("2026-09-16T06:30:00Z", 11.80, 99.79, "blitzortung")
    check("dedupe: ลูกแรกเก็บ", dedupe.is_new(first))
    check("dedupe: อีกเครือข่ายรายงานลูกเดียวกัน (1 วิ, ~2 กม.) = ตัดทิ้ง", not dedupe.is_new(same_other_net))
    check("dedupe: เวลาเดียวกันแต่ห่าง ~55 กม. = คนละลูก", dedupe.is_new(far))
    check("dedupe: ตำแหน่งเดียวกันแต่ห่าง 5 วินาที = คนละลูก", dedupe.is_new(later))
    check("dedupe: เครือข่ายเดียวกันไม่ตัด (เป็นฟ้าผ่าหลายลูกจริง)", dedupe.is_new(same_net))

    received: list[Strike] = []
    feeder = _FakeSource("blitzortung", True, True)
    other = _FakeSource("xweather", True, False)

    async def emit_b(on_strikes):
        on_strikes([first])

    async def emit_x(on_strikes):
        on_strikes([same_other_net, far])

    feeder.run, other.run = emit_b, emit_x
    asyncio.run(MergedSource([feeder, other]).run(received.extend))
    check("merged.run: ส่งต่อเฉพาะฟ้าผ่าที่ไม่ซ้ำ", len(received) == 2, str(len(received)))


def main() -> None:
    test_fusion()
    test_sparse_fusion()
    test_blitzortung_availability()
    test_merged_source()
    test_xweather_errors()
    test_xweather_parses_success()
    test_xweather_availability()
    for name in _passed:
        print(f"  ok  {name}")
    print(f"\nผ่านทั้งหมด {len(_passed)} ข้อ")


if __name__ == "__main__":
    main()
