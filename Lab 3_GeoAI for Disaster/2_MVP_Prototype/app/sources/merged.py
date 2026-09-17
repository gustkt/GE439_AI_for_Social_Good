"""รวมหลายแหล่งข้อมูลฟ้าผ่าให้ทำงานพร้อมกัน เช่น LIGHTNING_SOURCE=blitzortung,xweather

เหตุผล: ไม่มีแหล่งฟรีแหล่งเดียวที่ครบในไทย
  * Blitzortung  ฟรี ไม่มี quota ส่งข้อมูลสดตลอด แต่สถานีในไทยน้อย จับได้ไม่ครบ
  * Xweather     แม่นและครบกว่า แต่มี quota (หมดเมื่อไรก็หยุดส่ง)
รวมกันแล้ว: ระบบมีข้อมูลสดเสมอจาก Blitzortung และได้ความครบจาก Xweather เมื่อ quota ยังเหลือ

กติกา
  * พร้อมใช้ = มีอย่างน้อยหนึ่งแหล่งพร้อม
  * "จับได้ไม่ครบ" (sparse) = ไม่มีแหล่งที่ครบ (ไม่ sparse) ตัวไหนพร้อมใช้อยู่เลย
  * ฟ้าผ่าลูกเดียวกันที่สองเครือข่ายรายงาน (เวลาต่างกัน ≤ 1 วินาที ระยะ ≤ 5 กม.)
    เก็บครั้งเดียว ไม่งั้นจำนวนฟ้าผ่าและ nowcast จะเพี้ยน
  * แหล่งหนึ่งพังถาวร (เช่น credential ผิด) แหล่งอื่นยังทำงานต่อ
"""

from __future__ import annotations

import asyncio
import logging
from collections import defaultdict

from .. import config
from ..geo import haversine_km
from ..timeutil import parse_iso
from .base import LightningSource, OnStrikes, Strike

log = logging.getLogger(__name__)

# จำฟ้าผ่าย้อนหลังไว้เทียบซ้ำนานเท่านี้ (ตามเวลาที่เกิด) - Xweather ส่งข้อมูลย้อนหลังได้ถึง 5 นาที
_MEMORY_SECONDS = 15 * 60


class StrikeDeduplicator:
    """กันฟ้าผ่าลูกเดียวกันจากคนละแหล่ง - จัดกลุ่มตามวินาทีเพื่อเทียบเฉพาะช่วงเวลาใกล้กัน"""

    def __init__(self, seconds: float | None = None, km: float | None = None) -> None:
        self.seconds = config.MERGE_DEDUPE_SECONDS if seconds is None else seconds
        self.km = config.MERGE_DEDUPE_KM if km is None else km
        self._buckets: dict[int, list[tuple[float, float, float, str]]] = defaultdict(list)
        self._newest = 0.0

    def is_new(self, strike: Strike) -> bool:
        moment = parse_iso(strike.ts_iso)
        if moment is None:
            return True
        epoch = moment.timestamp()
        span = int(self.seconds) + 1
        second = int(epoch)
        for bucket in range(second - span, second + span + 1):
            for other_epoch, lat, lon, source in self._buckets.get(bucket, ()):
                if (
                    source != strike.source
                    and abs(other_epoch - epoch) <= self.seconds
                    and haversine_km(lat, lon, strike.lat, strike.lon) <= self.km
                ):
                    return False
        self._buckets[second].append((epoch, strike.lat, strike.lon, strike.source))
        if epoch > self._newest:
            self._newest = epoch
            self._prune()
        return True

    def _prune(self) -> None:
        cutoff = int(self._newest - _MEMORY_SECONDS)
        for bucket in [b for b in self._buckets if b < cutoff]:
            del self._buckets[bucket]


class MergedSource(LightningSource):
    name = "merged"

    def __init__(self, sources: list[LightningSource], build_errors: dict[str, str] | None = None) -> None:
        if not sources:
            raise RuntimeError("ไม่มีแหล่งข้อมูลฟ้าผ่าที่ใช้งานได้เลย")
        self.sources = sources
        self.build_errors = dict(build_errors or {})
        self.failed: dict[str, str] = {}
        self.duplicates_dropped = 0
        self._dedupe = StrikeDeduplicator()
        self.name = "+".join(source.name for source in sources)
        self.label = " + ".join(source.label for source in sources)

    async def run(self, on_strikes: OnStrikes) -> None:
        def forward(strikes: list[Strike]) -> None:
            fresh = [strike for strike in strikes if self._dedupe.is_new(strike)]
            self.duplicates_dropped += len(strikes) - len(fresh)
            if fresh:
                on_strikes(fresh)

        await asyncio.gather(*(self._run_one(source, forward) for source in self.sources))

    async def _run_one(self, source: LightningSource, forward: OnStrikes) -> None:
        try:
            await source.run(forward)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - แหล่งหนึ่งพังต้องไม่ลากแหล่งอื่นล่มไปด้วย
            self.failed[source.name] = str(exc)
            log.error("แหล่งข้อมูลฟ้าผ่า %s หยุดทำงาน (แหล่งอื่นยังทำงานต่อ): %s", source.name, exc)

    def _usable(self) -> list[LightningSource]:
        return [s for s in self.sources if s.name not in self.failed and s.availability()[0]]

    def availability(self) -> tuple[bool, str | None]:
        if self._usable():
            return True, None
        reasons = [f"{s.name}: {self.failed.get(s.name) or s.availability()[1]}" for s in self.sources]
        reasons += [f"{name}: {error}" for name, error in self.build_errors.items()]
        return False, " / ".join(reasons)

    def coverage_sparse(self) -> bool:
        usable = self._usable()
        return not any(not s.coverage_sparse() for s in usable)

    def monitored_stadium_ids(self) -> set[int] | None:
        watched: set[int] = set()
        for source in self.sources:
            ids = source.monitored_stadium_ids()
            if ids is None:
                return None
            watched |= ids
        return watched

    def status(self) -> dict:
        return {
            "sources": {
                s.name: {
                    **s.status(),
                    "available": s.availability()[0] and s.name not in self.failed,
                    "unavailable_reason": self.failed.get(s.name) or s.availability()[1],
                    "sparse_coverage": s.coverage_sparse(),
                }
                for s in self.sources
            },
            "not_started": self.build_errors,
            "duplicates_dropped": self.duplicates_dropped,
        }
