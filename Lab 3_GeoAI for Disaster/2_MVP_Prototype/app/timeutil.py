"""ยูทิลิตี้เรื่องเวลา - ทั้งระบบใช้ UTC อย่างเดียว

เวลาทุกค่าที่เก็บลง SQLite เป็นสตริง ISO-8601 UTC รูปแบบเดียวกันเป๊ะ
(`YYYY-MM-DDTHH:MM:SSZ`) เพื่อให้เปรียบเทียบด้วย `>=` / `<` ในระดับสตริงได้
ตรงกับการเรียงตามเวลาจริง ซึ่งทำให้ index `(stadium_id, ts_iso)` ใช้งานได้เต็มที่
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def to_iso(dt: datetime) -> str:
    """datetime -> สตริง ISO UTC รูปแบบมาตรฐานของโปรเจกต์"""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(ISO_FMT)


def now_iso() -> str:
    return to_iso(utcnow())


def iso_minutes_ago(minutes: float, now: datetime | None = None) -> str:
    return to_iso((now or utcnow()) - timedelta(minutes=minutes))


def parse_iso(value: str) -> datetime | None:
    """แปลงสตริงเวลาจาก provider เป็น datetime (tz-aware, UTC)

    รองรับทั้ง `...Z`, `...+07:00` และรูปแบบที่ไม่มี timezone
    (กรณีไม่มี timezone ให้ถือว่าเป็น UTC)
    """
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def normalize_iso(value: str) -> str | None:
    """สตริงเวลาจาก provider -> รูปแบบมาตรฐานของโปรเจกต์ (None ถ้าแปลงไม่ได้)"""
    dt = parse_iso(value)
    return to_iso(dt) if dt else None


def minutes_since(value: str, now: datetime | None = None) -> float | None:
    """ผ่านมากี่นาทีแล้วนับจากเวลาในสตริง (None ถ้าแปลงไม่ได้)"""
    dt = parse_iso(value)
    if dt is None:
        return None
    return ((now or utcnow()) - dt).total_seconds() / 60.0
