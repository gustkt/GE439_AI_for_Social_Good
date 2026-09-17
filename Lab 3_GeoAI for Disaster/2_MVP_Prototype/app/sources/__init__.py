"""แหล่งข้อมูลฟ้าผ่า - เลือกด้วย env var LIGHTNING_SOURCE

  blitzortung : ฟรี ไม่มี quota - stream real-time แต่สถานีในไทยน้อย จับได้ไม่ครบ
  xweather    : polling มี quota ต้องมี client_id + secret - ครบและแม่นกว่า
  replay      : เล่นข้อมูลจริงที่ capture ไว้ (ต้องตั้ง REPLAY_FILE)

ใช้หลายแหล่งพร้อมกันได้ คั่นด้วยจุลภาค (ค่าเริ่มต้น): blitzortung,xweather
-> ข้อมูลสดตลอดจาก Blitzortung + ความครบจาก Xweather เมื่อ quota ยังเหลือ (ดู merged.py)

ระบบนี้ **ไม่มีโหมดข้อมูลจำลอง** ทุกตัวเลขที่แสดงผลมาจากฟ้าผ่าที่เกิดขึ้นจริง
(ตัวสร้างข้อมูลสังเคราะห์มีเฉพาะใน tests/ สำหรับตรวจความถูกต้องของสูตรคำนวณ)
"""

from __future__ import annotations

import logging

from .base import LightningSource, OnStrikes, Strike

__all__ = ["LightningSource", "OnStrikes", "Strike", "build_source", "SOURCE_NAMES"]

SOURCE_NAMES = ("blitzortung", "xweather", "replay")

log = logging.getLogger(__name__)


def _build_one(choice: str) -> LightningSource:
    """สร้างแหล่งข้อมูลตามชื่อ (import แบบ lazy เพื่อไม่ให้ต้องติดตั้งทุก library)"""
    if choice == "blitzortung":
        from .blitzortung import BlitzortungSource

        return BlitzortungSource()
    if choice == "xweather":
        from .xweather import XweatherSource

        return XweatherSource()
    if choice == "replay":
        from .replay import ReplaySource

        return ReplaySource()
    raise RuntimeError(
        f"LIGHTNING_SOURCE={choice!r} ไม่รู้จัก - เลือกได้: {', '.join(SOURCE_NAMES)}"
    )


def build_source(name: str | None = None) -> LightningSource:
    from .. import config

    choices = [part.strip() for part in (name or config.LIGHTNING_SOURCE).lower().split(",") if part.strip()]
    if not choices:
        raise RuntimeError("ยังไม่ได้ตั้ง LIGHTNING_SOURCE")
    if len(choices) == 1:
        return _build_one(choices[0])
    if "replay" in choices:
        raise RuntimeError("replay ใช้ร่วมกับแหล่งสดไม่ได้ - ตั้ง LIGHTNING_SOURCE=replay อย่างเดียว")

    from .merged import MergedSource

    sources: list[LightningSource] = []
    errors: dict[str, str] = {}
    for choice in dict.fromkeys(choices):
        try:
            sources.append(_build_one(choice))
        except RuntimeError as exc:
            # เช่น ไม่มี credential ของ Xweather - ใช้แหล่งที่เหลือต่อไป แต่แจ้งไว้ใน /api/meta
            errors[choice] = str(exc)
            log.warning("ข้ามแหล่งข้อมูล %s: %s", choice, exc)
    if not sources:
        raise RuntimeError(" / ".join(f"{k}: {v}" for k, v in errors.items()))
    if len(sources) == 1 and not errors:
        return sources[0]
    return MergedSource(sources, errors)
