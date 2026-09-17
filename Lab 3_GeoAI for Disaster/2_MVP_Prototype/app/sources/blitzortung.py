"""แหล่งข้อมูลหลัก: Blitzortung.org (community lightning network) แบบ real-time stream

⚠ ข้อควรรู้ก่อนใช้ (ต้องระบุในรายงาน)
  * Blitzortung ไม่มี API ทางการ ข้อมูลนี้คือ feed ที่หน้าแผนที่ของเขาใช้
    รูปแบบ wire protocol ไม่มีเอกสาร และอาจเปลี่ยนเมื่อไรก็ได้
  * เงื่อนไขของ Blitzortung: ข้อมูลใช้ได้เฉพาะงานไม่เชิงพาณิชย์ ห้ามใช้
    ตัดสินใจด้านความปลอดภัย และ third-party app ต้องเสิร์ฟข้อมูลผ่าน server
    ของตัวเอง ห้ามให้เบราว์เซอร์ต่อเข้า Blitzortung ตรง ๆ - ระบบนี้ backend
    รับ stream -> เก็บ DB -> เสิร์ฟ frontend จึงเป็นไปตามข้อหลังนี้
  * ความแม่นของตำแหน่งในไทยด้อยกว่าเครือข่ายเชิงพาณิชย์ เพราะสถานีในภูมิภาค
    มีน้อย ควร cross-check กับเรดาร์ฝนของกรมอุตุนิยมวิทยา (TMD)
  * ระบบนี้เป็นต้นแบบเชิงวิชาการเท่านั้น

Wire protocol (ศึกษาจาก github.com/kevinkiyosepyo/Lightning-Data-Pipeline-API
และเขียนใหม่เอง):
  1. ต่อ WebSocket ไปที่ wss://wsN.blitzortung.org/
  2. ส่งข้อความ {"a": 111} เพื่อขอรับ feed
  3. แต่ละ frame เป็นข้อความที่บีบอัดด้วย LZW -> คลายแล้วได้ JSON หนึ่ง strike
     ฟิลด์ที่ใช้: time (nanosecond epoch), lat, lon, pol, sig (รายการสถานี)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone

from .. import config
from ..timeutil import now_iso, to_iso
from .base import LightningSource, OnStrikes, Strike

log = logging.getLogger(__name__)

# strike ไหลเข้ามาเป็นรายตัว รวบเป็นก้อนทุก ~1 วินาทีก่อนส่งให้ ingest
# เพื่อลดจำนวนครั้งที่เขียน SQLite
_BATCH_SECONDS = 1.0


def lzw_decode(data: str) -> str:
    """คลายการบีบอัด LZW แบบที่ Blitzortung ใช้

    ทุกตัวอักษรใน frame คือ "รหัส" หนึ่งตัว
      * codepoint < 256  -> เป็นตัวอักษรนั้นตรง ๆ
      * codepoint >= 256 -> อ้างถึงวลีในพจนานุกรม ที่สร้างขึ้นระหว่างคลาย
    พจนานุกรมเริ่มที่รหัส 256 และทุกก้าวจะเพิ่มวลีใหม่ = วลีก่อนหน้า + ตัวแรกของวลีปัจจุบัน
    กรณีพิเศษ (cScSc): รหัสที่ยังไม่มีในพจนานุกรม = วลีก่อนหน้า + ตัวแรกของมันเอง
    """
    if not data:
        return ""
    phrases: dict[int, str] = {}
    previous = data[0]
    first_char = previous
    output = [previous]
    next_code = 256

    for char in data[1:]:
        code = ord(char)
        if code < 256:
            entry = char
        elif code in phrases:
            entry = phrases[code]
        else:
            entry = previous + first_char  # กรณี cScSc
        output.append(entry)
        first_char = entry[0]
        phrases[next_code] = previous + first_char
        next_code += 1
        previous = entry

    return "".join(output)


def parse_frame(frame: str | bytes) -> Strike | None:
    """frame ดิบหนึ่งอัน -> Strike (None ถ้าข้อมูลไม่ครบหรือ decode ไม่ได้)"""
    text = frame.decode("utf-8", errors="replace") if isinstance(frame, bytes) else frame
    try:
        payload = json.loads(lzw_decode(text))
    except (json.JSONDecodeError, IndexError, KeyError):
        return None
    if not isinstance(payload, dict):
        return None

    try:
        time_ns = int(payload["time"])
        lat = float(payload["lat"])
        lon = float(payload["lon"])
    except (KeyError, TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None

    signals = payload.get("sig")
    polarity = payload.get("pol")
    return Strike(
        ts_iso=to_iso(datetime.fromtimestamp(time_ns / 1_000_000_000, tz=timezone.utc)),
        lat=lat,
        lon=lon,
        source="blitzortung",
        polarity=int(polarity) if isinstance(polarity, (int, float)) else None,
        station_count=len(signals) if isinstance(signals, list) else None,
    )


class BlitzortungSource(LightningSource):
    name = "blitzortung"
    label = "BLITZORTUNG (LIVE · ฟรี)"
    # สถานีในไทยมีน้อย จับได้เฉพาะฟ้าผ่าที่แรงพอ -> ไม่เห็นฟ้าผ่าไม่ได้แปลว่าไม่มี
    sparse_coverage = True

    def __init__(self, hosts: list[str] | None = None) -> None:
        self._hosts = hosts or config.BLITZORTUNG_WS_HOSTS
        self.connected_host: str | None = None
        self.last_error: str | None = None
        self.frames_total = 0
        self.last_frame_at: str | None = None
        self._last_frame_mono: float | None = None
        self._connected_mono: float | None = None

    async def run(self, on_strikes: OnStrikes) -> None:
        import websockets  # import ตรงนี้ เพื่อให้โหมดอื่นรันได้แม้ไม่ได้ติดตั้ง

        attempt = 0
        while True:
            host = self._hosts[attempt % len(self._hosts)]
            url = f"wss://{host}/"
            try:
                async with websockets.connect(url, open_timeout=15, ping_interval=20) as ws:
                    await ws.send(json.dumps({"a": 111}))
                    self.connected_host = host
                    self._connected_mono = time.monotonic()
                    self.last_error = None
                    attempt = 0
                    log.info("เชื่อมต่อ Blitzortung สำเร็จ: %s", url)
                    await self._consume(ws, on_strikes)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - stream ต้องต่อใหม่เองได้เสมอ
                self.connected_host = None
                self.last_error = f"{type(exc).__name__}: {exc}"
                attempt += 1
                wait = min(60, 5 * attempt)
                log.warning(
                    "ต่อ Blitzortung (%s) ไม่ได้: %s - ลองใหม่ใน %s วินาที "
                    "(ถ้าต่อไม่ได้ต่อเนื่อง ให้สลับ LIGHTNING_SOURCE=xweather "
                    "หรือ replay ใน .env)",
                    host,
                    self.last_error,
                    wait,
                )
                await asyncio.sleep(wait)

    async def _consume(self, ws, on_strikes: OnStrikes) -> None:
        batch: list[Strike] = []
        last_flush = time.monotonic()
        async for frame in ws:
            self.frames_total += 1
            self.last_frame_at = now_iso()
            self._last_frame_mono = time.monotonic()
            strike = parse_frame(frame)
            if strike is not None:
                batch.append(strike)
            if batch and time.monotonic() - last_flush >= _BATCH_SECONDS:
                # เขียน DB เป็น I/O แบบ blocking - โยนไป thread แยก ไม่ให้ค้าง event loop
                await asyncio.to_thread(on_strikes, batch)
                batch = []
                last_flush = time.monotonic()

    def availability(self) -> tuple[bool, str | None]:
        if self.connected_host is None:
            return False, self.last_error or "ยังเชื่อมต่อ Blitzortung ไม่สำเร็จ"
        # feed เป็นระดับโลก ปกติมี frame เข้าทุกไม่กี่วินาที - เงียบนานแปลว่า stream ค้าง
        # ทั้งที่ socket ยังไม่หลุด ต้องถือว่าไม่มีข้อมูล ไม่ใช่ไม่มีฟ้าผ่า
        last = self._last_frame_mono or self._connected_mono
        if last is not None:
            silent = time.monotonic() - last
            if silent > config.BLITZORTUNG_STALE_SECONDS:
                return False, f"Blitzortung ไม่ส่งข้อมูลมา {silent / 60:.0f} นาที (stream ค้าง)"
        return True, None

    def status(self) -> dict:
        return {
            "connected_host": self.connected_host,
            "frames_total": self.frames_total,
            "last_frame_at": self.last_frame_at,
            "last_error": self.last_error,
        }
