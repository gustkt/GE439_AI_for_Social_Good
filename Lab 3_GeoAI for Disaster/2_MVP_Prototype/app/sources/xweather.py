"""แหล่งข้อมูลฟ้าผ่า: Xweather (Vaisala) Lightning API แบบ polling

ข้อจำกัด free tier ที่มีผลต่อการออกแบบ:
  * lightning มี multiplier x10 -> quota ต่อเดือนหมดเร็ว
  * ย้อนหลังได้แค่ 5 นาที (ทดสอบกับข้อมูลจริง 2026-09-14) -> ประวัติสำหรับ
    nowcast ต้องสะสมจากหลายรอบ poll
  * รัศมีสูงสุด 100 กม., สูงสุด 1000 event/คำขอ
  * action `within` (polygon/bbox) ใช้ไม่ได้ -> ใช้ `closest` (จุด + รัศมี)

การแยกประเภท error (สำคัญต่อความปลอดภัยของระบบ):
  * HTTP 429 + code "maxhits"  = quota ของรอบบิลหมด -> **ถาวร** หยุดยิง แจ้งว่าไม่พร้อม
                                 แล้วลองใหม่ทุก XWEATHER_RETRY_MINUTES นาที
  * HTTP 401 / invalid_client  = credential ผิด -> ถาวร เหมือนกัน
  * HTTP 429 แบบอื่น           = rate limit ชั่วคราว -> ข้ามรอบนี้
  เวอร์ชันก่อนหน้าเหมารวม 429 เป็น "ชั่วคราว" ทำให้ quota หมดแล้วยังยิงซ้ำทุก 4 วินาที
  และหน้าเว็บขึ้นทุกสนามเป็น "ปลอดภัย" ทั้งที่ไม่มีข้อมูลเลย (พบจริง 2026-09-16)

Auth: ต้องใช้ "คู่" query param client_id + client_secret เท่านั้น
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime

import httpx

from .. import config
from ..geo import haversine_km
from ..timeutil import normalize_iso, now_iso, utcnow
from .base import LightningSource, OnStrikes, Strike

log = logging.getLogger(__name__)

MAX_QUERY_RADIUS_KM = 100.0  # ขีดจำกัด free tier


class QuotaExhausted(RuntimeError):
    """quota ของรอบบิลหมด - ยิงต่อก็ไม่ได้ข้อมูล"""


class CredentialRejected(RuntimeError):
    """client_id / client_secret ใช้ไม่ได้"""


class RateLimited(RuntimeError):
    """ยิงถี่เกินชั่วคราว - รอบถัดไปใช้ได้ตามปกติ"""


@dataclass
class Cluster:
    """กลุ่มสนามที่ใช้ผลการ query ครั้งเดียวร่วมกันได้"""

    lat: float
    lon: float
    radius_km: float
    stadiums: list[dict] = field(default_factory=list)


def build_clusters(stadiums: list[dict], merge_km: float | None = None) -> list[Cluster]:
    """จัดกลุ่มสนามแบบ greedy เพื่อประหยัดโควตา

    สนามที่ห่างจากจุดยึดของกลุ่มไม่เกิน merge_km ใช้ query เดียวกัน เช่น
    Bangkok United กับ BG Pathum United ที่ใช้ True BG Stadium พิกัดเดียวกัน
    """
    merge = config.CLUSTER_MERGE_KM if merge_km is None else merge_km
    clusters: list[Cluster] = []
    for stadium in sorted(stadiums, key=lambda s: (s["lat"], s["lon"])):
        for cluster in clusters:
            if haversine_km(cluster.lat, cluster.lon, stadium["lat"], stadium["lon"]) <= merge:
                cluster.stadiums.append(stadium)
                break
        else:
            clusters.append(Cluster(stadium["lat"], stadium["lon"], 0.0, [stadium]))

    # รัศมี query ต้องครอบคลุมรัศมีเก็บข้อมูลของสมาชิกที่ไกลที่สุดในกลุ่ม
    for cluster in clusters:
        furthest = max(
            haversine_km(cluster.lat, cluster.lon, s["lat"], s["lon"]) for s in cluster.stadiums
        )
        cluster.radius_km = min(config.INGEST_RADIUS_KM + furthest, MAX_QUERY_RADIUS_KM)
    return clusters


class XweatherSource(LightningSource):
    name = "xweather"
    label = "XWEATHER (LIVE · FALLBACK)"

    def __init__(self) -> None:
        if not config.xweather_configured():
            raise RuntimeError(
                "LIGHTNING_SOURCE=xweather แต่ XWEATHER_CLIENT_ID / XWEATHER_CLIENT_SECRET "
                "ไม่ครบใน .env (endpoint นี้ต้องใช้ 'คู่' id+secret เท่านั้น)"
            )
        # all = ทุกสนามทุกรอบ, watch_ids = เฉพาะสนามที่เลือก, ไม่ตั้งทั้งคู่ = วนทีละกลุ่ม
        self.watch_all = config.XWEATHER_WATCH_ALL
        self.watch_ids: set[int] = set() if self.watch_all else set(config.XWEATHER_STADIUM_IDS)
        if self.watch_all:
            self.label = "XWEATHER (LIVE · ทุกสนาม)"
        elif self.watch_ids:
            self.label = "XWEATHER (LIVE · เฝ้าเฉพาะสนามที่เลือก)"
        self._cursor = 0
        self._quota_logged = False
        self.watching: list[str] = []
        self.calls_per_hour = 0.0
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self.calls_total = 0
        # ความพร้อมของข้อมูล: ใช้ตัดสินว่าจะตอบ ALL_CLEAR ได้หรือไม่ (ดู model/fusion.py)
        self.last_success: datetime | None = None
        self.unavailable_reason: str | None = None

    def monitored_stadium_ids(self) -> set[int] | None:
        # โหมด all และโหมดวนทีละกลุ่มครอบคลุมทุกสนาม -> None
        return set(self.watch_ids) or None

    def availability(self) -> tuple[bool, str | None]:
        if self.unavailable_reason:
            return False, self.unavailable_reason
        if self.last_success is None:
            return False, "กำลังรอข้อมูลรอบแรกจาก Xweather"
        age = (utcnow() - self.last_success).total_seconds()
        # โหมดวนทีละกลุ่ม แต่ละรอบใช้เวลานานกว่า จึงยอมให้ขาดช่วงได้นานกว่า
        limit = max(180, 3 * config.POLL_INTERVAL_SECONDS)
        if age > limit:
            return False, f"ไม่ได้รับข้อมูลจาก Xweather มา {age / 60:.0f} นาที"
        return True, None

    def _mark_success(self) -> None:
        if self.unavailable_reason:
            log.info("Xweather กลับมาใช้งานได้แล้ว")
        self.unavailable_reason = None
        self.last_error = None
        self.last_success = utcnow()

    def _mark_unavailable(self, reason: str) -> None:
        if reason != self.unavailable_reason:
            log.error(
                "Xweather ใช้งานไม่ได้: %s - หยุดยิงและจะลองใหม่ทุก %s นาที",
                reason,
                config.XWEATHER_RETRY_MINUTES,
            )
        self.unavailable_reason = reason
        self.last_error = reason

    async def run(self, on_strikes: OnStrikes) -> None:
        from .. import db  # import ตอนใช้ เลี่ยง circular import (db -> sources.base)

        loop = asyncio.get_running_loop()
        async with httpx.AsyncClient(timeout=20.0) as http:
            while True:
                stadiums = await asyncio.to_thread(db.list_stadiums, True)
                if self.watch_ids:
                    stadiums = [s for s in stadiums if s["id"] in self.watch_ids]
                clusters = build_clusters(stadiums)
                if not clusters:
                    self._mark_unavailable(
                        f"XWEATHER_STADIUM_IDS={sorted(self.watch_ids)} ไม่ตรงกับสนาม active ใดเลย"
                    )
                    await asyncio.sleep(max(10, config.POLL_INTERVAL_SECONDS))
                    continue

                if self.watch_all or self.watch_ids:
                    # ยิงทุกกลุ่มทุกรอบ API ย้อนหลังได้ 5 นาที poll ทุก 60 วิ ข้อมูลแต่ละรอบ
                    # จึงซ้อนกัน ประวัติไม่ขาดช่วง (ตัวซ้ำถูกกรองที่ DB)
                    per_tick = len(clusters)
                else:
                    per_tick = max(1, config.POLL_CLUSTERS_PER_TICK or 1)
                per_tick = min(per_tick, len(clusters))
                self._log_quota_once(per_tick, stadiums)

                # กระจายคำขอเท่า ๆ กันตลอดรอบ แทนการยิงรัวติดกัน ลดโอกาสโดน rate limit
                spacing = max(10, config.POLL_INTERVAL_SECONDS) / per_tick
                for _ in range(per_tick):
                    started = loop.time()
                    cluster = clusters[self._cursor % len(clusters)]
                    self._cursor += 1
                    try:
                        strikes = await self.fetch(http, cluster.lat, cluster.lon, cluster.radius_km)
                    except (QuotaExhausted, CredentialRejected) as exc:
                        # ปัญหาถาวร: ยิงต่อก็ได้ error เดิม หยุดทั้งรอบแล้วค่อยลองใหม่ภายหลัง
                        self._mark_unavailable(str(exc))
                        self.last_poll_at = now_iso()
                        await asyncio.sleep(max(60, config.XWEATHER_RETRY_MINUTES * 60))
                        break
                    except Exception as exc:  # noqa: BLE001 - poll รอบหน้าต้องยังเดินต่อ
                        self.last_error = str(exc)
                        log.warning("poll Xweather ล้มเหลว: %s", exc)
                    else:
                        self._mark_success()
                        if strikes:
                            await asyncio.to_thread(on_strikes, strikes)
                    self.last_poll_at = now_iso()
                    await asyncio.sleep(max(0.0, spacing - (loop.time() - started)))

    async def fetch(
        self, http: httpx.AsyncClient, lat: float, lon: float, radius_km: float
    ) -> list[Strike]:
        params = {
            "p": f"{lat:.6f},{lon:.6f}",
            "radius": f"{radius_km:g}km",
            "limit": str(config.POLL_LIMIT),
            "format": "geojson",
            "client_id": config.XWEATHER_CLIENT_ID,
            "client_secret": config.XWEATHER_CLIENT_SECRET,
        }
        response = await http.get(config.XWEATHER_BASE_URL, params=params)
        self.calls_total += 1
        code, description = _error_of(response)

        if response.status_code == 401 or code == "invalid_client":
            raise CredentialRejected(
                "Xweather ปฏิเสธ credential - ตรวจ XWEATHER_CLIENT_ID และ XWEATHER_CLIENT_SECRET ใน .env"
            )
        if code == "maxhits":
            raise QuotaExhausted(
                f"quota ของ Xweather หมดแล้ว ({description or 'maxhits'}) "
                "ใช้ได้อีกครั้งเมื่อรอบ quota ถัดไปเริ่ม"
            )
        if response.status_code == 429:
            raise RateLimited(f"Xweather rate limit ชั่วคราว (429 {code or ''})".strip())
        response.raise_for_status()

        payload = response.json()
        if isinstance(payload, dict) and payload.get("success") is False:
            if code.startswith("warn_"):  # ไม่มีข้อมูลในพื้นที่ = ปกติ
                return []
            raise RuntimeError(f"Xweather error: {code} - {description}")

        strikes = []
        for record in _iter_records(payload):
            strike = _parse_record(record, lat, lon)
            if strike is not None:
                strikes.append(strike)
        return strikes

    def _log_quota_once(self, calls_per_tick: int, stadiums: list[dict]) -> None:
        """เตือนครั้งเดียวว่าโหมดนี้กิน quota เท่าไร"""
        self.watching = sorted({s["name"] for s in stadiums})
        self.calls_per_hour = calls_per_tick * 3600 / max(10, config.POLL_INTERVAL_SECONDS)
        if self._quota_logged:
            return
        self._quota_logged = True
        log.warning(
            "Xweather: เฝ้า %s สนาม (%s) ยิงประมาณ %.0f call/ชม. - free tier ของ lightning "
            "มี multiplier x10 ปิดเซิร์ฟเวอร์เมื่อไม่ใช้ เพื่อประหยัด quota",
            len(stadiums),
            ", ".join(self.watching),
            self.calls_per_hour,
        )

    def status(self) -> dict:
        available, reason = self.availability()
        return {
            "available": available,
            "unavailable_reason": reason,
            "watching": self.watching,
            "calls_per_hour": round(self.calls_per_hour),
            "last_poll_at": self.last_poll_at,
            "last_success_at": self.last_success.isoformat() if self.last_success else None,
            "last_error": self.last_error,
            "calls_total": self.calls_total,
        }


def _error_of(response: httpx.Response) -> tuple[str, str]:
    """ดึง (error.code, error.description) จาก body ถ้ามี - ไม่มีคืนสตริงว่าง"""
    try:
        payload = response.json()
    except ValueError:
        return "", ""
    if not isinstance(payload, dict):
        return "", ""
    error = payload.get("error") or {}
    if not isinstance(error, dict):
        return "", ""
    return str(error.get("code") or ""), str(error.get("description") or "")


def _iter_records(payload: object):
    """ดึง record ออกจาก payload ทั้งแบบ geojson (features) และ json (response)"""
    if not isinstance(payload, dict):
        return
    for feature in payload.get("features") or []:
        if isinstance(feature, dict):
            props = feature.get("properties")
            record = props if isinstance(props, dict) and props else feature
            yield {**record, "_geometry": feature.get("geometry") or {}}
    for record in payload.get("response") or []:
        if isinstance(record, dict):
            yield record


def _parse_record(record: dict, query_lat: float, query_lon: float) -> Strike | None:
    """แปลง record ดิบของ Xweather เป็น Strike กลาง"""
    ob = record.get("ob") or {}
    loc = record.get("loc") or {}
    relative_to = record.get("relativeTo") or {}
    pulse = ob.get("pulse") or {}

    ts_iso = normalize_iso(ob.get("dateTimeISO") or record.get("dateTimeISO") or "")
    if ts_iso is None:
        return None

    lat, lon = _num(loc.get("lat")), _num(loc.get("long"))
    if lat is None or lon is None:
        coords = (record.get("_geometry") or {}).get("coordinates")
        if isinstance(coords, (list, tuple)) and len(coords) >= 2:
            lon, lat = _num(coords[0]), _num(coords[1])
    if lat is None or lon is None:
        return None

    peak_amp = _num(pulse.get("peakamp"))
    sensors = _num(pulse.get("numSensors"))
    return Strike(
        ts_iso=ts_iso,
        lat=lat,
        lon=lon,
        source="xweather",
        polarity=(1 if peak_amp > 0 else -1) if peak_amp else None,
        station_count=int(sensors) if sensors is not None else None,
        # ระยะที่ API คำนวณจากจุด query (relativeTo.distanceKM) - ingest จะใช้ตรง ๆ
        # เมื่อจุด query คือตัวสนาม ตามที่โจทย์กำหนด
        ref_lat=query_lat,
        ref_lon=query_lon,
        ref_distance_km=_num(relative_to.get("distanceKM")),
    )


def _num(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None
