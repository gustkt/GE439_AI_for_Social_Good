"""รวมหลักฐานจากหลายแหล่งเป็นสถานะเดียวของสนาม (data fusion)

หลักการ (ตกลงไว้ในการออกแบบ):
  * ฟ้าผ่า = หลักฐานสั่งการ -> เป็นตัวเดียวที่ทำให้ขึ้น SUSPEND / DANGER
    เพราะเกณฑ์ NCAA (13 กม. / 30 นาที) นิยามจากระยะฟ้าผ่า ไม่ใช่ฝน
  * เรดาร์ = สัญญาณเตือนล่วงหน้า -> cell ฝนแรงใกล้สนามทำให้ขึ้น WATCH ได้
    แม้ข้อมูลฟ้าผ่าจะยังไม่เห็นอะไร (ลดจุดบอดของเครือข่ายตรวจฟ้าผ่า)
  * "ไม่มีข้อมูล" ไม่ใช่ "ปลอดภัย" -> จะตอบ ALL_CLEAR ได้ก็ต่อเมื่อแหล่งข้อมูลฟ้าผ่า
    พร้อมใช้จริง ถ้าไม่พร้อม (quota หมด / หลุดการเชื่อมต่อ / ข้อมูลขาดช่วง)
    และไม่มีหลักฐานอื่น สถานะจะเป็น NO_DATA
  * เครือข่ายที่ตรวจจับได้ไม่ครบ (เช่น Blitzortung ในไทย สถานีน้อย) -> "ไม่เห็นฟ้าผ่า"
    ยังไม่พอจะบอกว่าปลอดภัย ต้องมีเรดาร์ยืนยันว่าไม่มีฝนแรงด้วย ถ้าเรดาร์ไม่พร้อม = NO_DATA
  * การขาดข้อมูลใหม่ไม่ลบหลักฐานเดิม: strike ที่อยู่ใน DB แล้วยังนับตามปกติ
    เช่น quota หมดกลางพายุ สนามที่ SUSPEND อยู่จะไม่กลายเป็น NO_DATA
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: ลำดับความรุนแรงสำหรับเรียงผล (NO_DATA สำคัญกว่า ALL_CLEAR เพราะต้องมีคนไปตรวจ)
RISK_ORDER = ["DANGER", "SUSPEND", "WATCH", "NO_DATA", "ALL_CLEAR", "UNMONITORED"]


@dataclass
class FusedStatus:
    status: str
    reasons: list[str] = field(default_factory=list)


def fuse(
    lightning_status: str,
    lightning_available: bool,
    lightning_reason: str | None = None,
    radar_watch: bool = False,
    radar_reason: str | None = None,
    radar_available: bool = True,
    radar_unavailable_reason: str | None = None,
    lightning_sparse: bool = False,
) -> FusedStatus:
    """ตัดสินสถานะสุดท้ายจากสถานะตามฟ้าผ่า + ความพร้อมของข้อมูล + เรดาร์

    lightning_status    : ผลจาก tiering.evaluate_tier (DANGER/SUSPEND/WATCH/ALL_CLEAR)
    lightning_available : แหล่งข้อมูลฟ้าผ่าพร้อมใช้อยู่หรือไม่
    radar_watch         : มี cell ฝนแรงในวง WATCH หรือไม่
    lightning_sparse    : ข้อมูลฟ้าผ่ามาจากเครือข่ายที่ตรวจจับได้ไม่ครบหรือไม่
    """
    if lightning_status in ("DANGER", "SUSPEND"):
        return FusedStatus(lightning_status, ["มี strike ยืนยันในวงสั่งหยุด"])

    reasons = []
    if lightning_status == "WATCH":
        reasons.append("มี strike ในวงเฝ้าระวัง")
    if radar_watch:
        reasons.append(radar_reason or "เรดาร์พบ cell ฝนแรงในวงเฝ้าระวัง")
    if reasons:
        if not lightning_available:
            reasons.append(f"ข้อมูลฟ้าผ่าไม่พร้อม: {lightning_reason or 'ไม่ทราบสาเหตุ'}")
        elif lightning_sparse and lightning_status != "WATCH":
            reasons.append("เครือข่ายฟ้าผ่าตรวจจับในไทยได้ไม่ครบ ฟ้าผ่าที่อ่อนอาจไม่ปรากฏ")
        return FusedStatus("WATCH", reasons)

    if not lightning_available:
        return FusedStatus("NO_DATA", [f"ข้อมูลฟ้าผ่าไม่พร้อม: {lightning_reason or 'ไม่ทราบสาเหตุ'}"])

    if lightning_sparse:
        if not radar_available:
            return FusedStatus("NO_DATA", [
                "เครือข่ายฟ้าผ่าตรวจจับในไทยได้ไม่ครบ ต้องมีเรดาร์ยืนยันว่าไม่มีฝนแรงก่อนตอบว่าปลอดภัย",
                f"เรดาร์ไม่พร้อม: {radar_unavailable_reason or 'ไม่ทราบสาเหตุ'}",
            ])
        return FusedStatus("ALL_CLEAR", ["ไม่พบฟ้าผ่า และเรดาร์ไม่พบฝนแรงในวงเฝ้าระวัง"])

    if not radar_available:
        # ฟ้าผ่ายืนยันแล้วว่าไม่มี strike แต่ไม่มีเรดาร์ช่วยดูฝนที่กำลังก่อตัว - บอกให้รู้
        return FusedStatus("ALL_CLEAR", [f"ไม่มีข้อมูลเรดาร์ประกอบ: {radar_unavailable_reason or 'ไม่ทราบสาเหตุ'}"])
    return FusedStatus("ALL_CLEAR", [])
