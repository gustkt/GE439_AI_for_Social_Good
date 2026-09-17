"""สัญญากลาง (interface) ของทุกแหล่งข้อมูลฟ้าผ่า

รองรับทั้งสองแบบด้วย method เดียวคือ `run(on_strikes)`
  * push (stream) : BlitzortungSource ต่อ WebSocket ค้างไว้ แล้วเรียก
                    on_strikes ทุกครั้งที่มี strike ไหลเข้ามา
  * pull (polling): XweatherSource วน poll ทุก N วินาที แล้วเรียก
                    on_strikes หนึ่งครั้งต่อรอบ
  * replay        : อ่านไฟล์ข้อมูลจริงที่บันทึกไว้ แล้วเรียก on_strikes ตามเวลาเดิม

ส่วนที่เหลือของระบบ (ingest / db / model / api) รู้จักแค่ `Strike` กับ
callback นี้ จึงสลับแหล่งข้อมูลได้โดยไม่ต้องแก้โค้ดส่วนอื่น
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class Strike:
    """ฟ้าผ่าหนึ่งครั้ง ในรูปแบบกลางที่ไม่ผูกกับแหล่งข้อมูลรายใด

    ts_iso        : เวลาที่ฟ้าผ่าเกิดจริง (ISO-8601 UTC ลงท้าย Z)
                    **ไม่ใช่** เวลาที่เรารับข้อมูล - nowcast bin ตามค่านี้
    lat, lon      : ตำแหน่งที่เกิด
    source        : 'blitzortung' | 'xweather' | 'replay:<ไฟล์>'
    polarity      : ขั้วของประจุ ถ้าแหล่งข้อมูลให้มา
    station_count : จำนวนสถานีที่ตรวจจับได้ (มากขึ้น = ตำแหน่งน่าเชื่อถือขึ้น)
    ref_lat/ref_lon/ref_distance_km :
                    ระยะที่ผู้ให้บริการคำนวณมาให้ และจุดที่ใช้วัด
                    (Xweather ส่ง relativeTo.distanceKM) ingest จะใช้ค่านี้
                    เฉพาะเมื่อจุดอ้างอิงตรงกับสนาม ไม่งั้นคำนวณ haversine เอง
    """

    ts_iso: str
    lat: float
    lon: float
    source: str
    polarity: int | None = None
    station_count: int | None = None
    ref_lat: float | None = None
    ref_lon: float | None = None
    ref_distance_km: float | None = None

    def to_dict(self) -> dict:
        return asdict(self)


#: callback กลางที่ทุกแหล่งข้อมูลเรียกเมื่อมี strike ใหม่ (ดู app/ingest.py)
OnStrikes = Callable[[list[Strike]], None]


class LightningSource(ABC):
    """Interface ที่แหล่งข้อมูลทุกตัวต้องทำตาม"""

    #: ชื่อสั้น ๆ ใช้ใน log และคอลัมน์ strikes.source
    name: str = "base"

    #: ข้อความบน badge ของ frontend
    label: str = "UNKNOWN"

    #: True = เครือข่ายตรวจจับได้ไม่ครบในพื้นที่ (สถานีน้อย) "ไม่เห็นฟ้าผ่า" จึงยังไม่พอ
    #: จะตอบ ALL_CLEAR ต้องมีเรดาร์ยืนยันด้วยว่าไม่มีฝนแรง (ดู app/model/fusion.py)
    sparse_coverage: bool = False

    @abstractmethod
    async def run(self, on_strikes: OnStrikes) -> None:
        """ทำงานต่อเนื่องจนกว่าจะถูก cancel แล้วเรียก on_strikes เมื่อมี strike ใหม่

        ต้องจัดการ reconnect / error ชั่วคราวเอง ห้ามปล่อยให้หลุดออกมา
        เว้นแต่เป็น error ที่แก้ไม่ได้จริง (เช่น credential ผิด)
        """

    def status(self) -> dict:
        """สถานะสำหรับ /api/meta เช่น เชื่อมต่ออยู่ไหม รับ strike ล่าสุดเมื่อไร"""
        return {}

    def availability(self) -> tuple[bool, str | None]:
        """(พร้อมใช้หรือไม่, เหตุผลถ้าไม่พร้อม)

        สำคัญต่อความปลอดภัย: ถ้าแหล่งข้อมูลไม่พร้อม ระบบจะไม่ตอบ ALL_CLEAR
        เพราะ "ไม่เห็นฟ้าผ่า" กับ "ไม่มีข้อมูล" ต่างกัน (ดู app/model/fusion.py)
        """
        return True, None

    def coverage_sparse(self) -> bool:
        """ข้อมูลที่ใช้อยู่ตอนนี้มาจากเครือข่ายที่ตรวจจับได้ไม่ครบหรือไม่"""
        return self.sparse_coverage

    def monitored_stadium_ids(self) -> set[int] | None:
        """id สนามที่แหล่งข้อมูลนี้ติดตามอยู่ - None = ทุกสนาม active

        แหล่งที่เฝ้าแค่บางสนาม (เช่น Xweather ที่ตั้ง XWEATHER_STADIUM_IDS) ต้อง
        override เพื่อให้ API แสดงสนามอื่นเป็น UNMONITORED แทนที่จะเป็น ALL_CLEAR
        """
        return None
