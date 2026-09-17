"""สร้างเอกสารส่งอาจารย์เป็นไฟล์ Word จากรูปและตัวเลขที่คำนวณจากข้อมูลจริง

รัน (จากโฟลเดอร์โปรเจกต์ ตามลำดับ):
  python -m docs.build.make_figures     กราฟ + stats.json
  python -m docs.build.make_visuals     diagram + ภาพหน้าจอ (ต้องเปิดเซิร์ฟเวอร์)
  python -m docs.build.make_docx        ไฟล์ Word

ผลลัพธ์ (submission/):
  1_System_Design.docx   System Design 2–3 หน้า + diagram
  3_Report.docx          รายงานผล 5–7 หน้า + รูปผลลัพธ์

ตัวเลขในเนื้อหาอ่านจาก docs/figures/stats.json ทั้งหมด ไม่ได้พิมพ์ใส่เอง
"""

from __future__ import annotations

import json
from pathlib import Path

from docx import Document
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_BREAK
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor

ROOT = Path(__file__).resolve().parents[2]
FIG = ROOT / "docs" / "figures"
OUT = ROOT / "submission"
FONT = "TH Sarabun New"
BODY_PT = 15
PLACEHOLDER = RGBColor(0x99, 0x99, 0x99)
ACCENT = RGBColor(0x1F, 0x4E, 0x79)

STATUS_THAI = {
    "DANGER": "อันตราย (DANGER)",
    "SUSPEND": "สั่งหยุด (SUSPEND)",
    "WATCH": "เฝ้าระวัง (WATCH)",
    "ALL_CLEAR": "ปลอดภัย (ALL_CLEAR)",
    "NO_DATA": "ไม่มีข้อมูล (NO_DATA)",
}


# ================================================================== helpers


def _font_xml(rpr, size: float | None = None, bold: bool | None = None) -> None:
    """ตั้งฟอนต์ให้ครบทุกชนิดตัวอักษร - ภาษาไทยใช้ฟอนต์ complex script (w:cs)"""
    fonts = rpr.find(qn("w:rFonts"))
    if fonts is None:
        fonts = OxmlElement("w:rFonts")
        rpr.insert(0, fonts)
    for attr in ("w:asciiTheme", "w:hAnsiTheme", "w:eastAsiaTheme", "w:cstheme"):
        if fonts.get(qn(attr)) is not None:
            del fonts.attrib[qn(attr)]
    for attr in ("w:ascii", "w:hAnsi", "w:cs", "w:eastAsia"):
        fonts.set(qn(attr), FONT)
    if size is not None:
        for tag in ("w:sz", "w:szCs"):
            element = rpr.find(qn(tag))
            if element is None:
                element = OxmlElement(tag)
                rpr.append(element)
            element.set(qn("w:val"), str(int(size * 2)))
    if bold is not None:
        for tag in ("w:b", "w:bCs"):
            element = rpr.find(qn(tag))
            if bold and element is None:
                rpr.append(OxmlElement(tag))
            elif not bold and element is not None:
                rpr.remove(element)
    lang = rpr.find(qn("w:lang"))
    if lang is None:
        lang = OxmlElement("w:lang")
        rpr.append(lang)
    lang.set(qn("w:bidi"), "th-TH")


def style_run(run, size: float = BODY_PT, bold: bool = False, italic: bool = False, color: RGBColor | None = None):
    run.font.size = Pt(size)
    run.font.bold = bold
    run.font.italic = italic
    if color is not None:
        run.font.color.rgb = color
    _font_xml(run._element.get_or_add_rPr(), size, bold)
    return run


def new_document() -> Document:
    doc = Document()
    section = doc.sections[0]
    section.page_width, section.page_height = Cm(21), Cm(29.7)
    section.left_margin = section.right_margin = Cm(2.2)
    section.top_margin = section.bottom_margin = Cm(1.8)

    for name, size, bold in (
        ("Normal", BODY_PT, False), ("List Bullet", BODY_PT, False), ("List Number", BODY_PT, False),
        ("Title", 24, True), ("Heading 1", 19, True), ("Heading 2", 16.5, True),
    ):
        style = doc.styles[name]
        style.font.name = FONT
        style.font.size = Pt(size)
        style.font.bold = bold
        _font_xml(style.element.get_or_add_rPr(), size, bold)
        style.paragraph_format.space_after = Pt(2)
        style.paragraph_format.line_spacing = 1.0
        if name.startswith("Heading") or name == "Title":
            style.font.color.rgb = ACCENT
            style.paragraph_format.space_before = Pt(8 if name == "Heading 1" else 4)
            style.paragraph_format.keep_with_next = True
    add_page_number(section)
    # เทมเพลตตั้งต้นของ python-docx มี <w:zoom> ที่ขาด w:percent (ผิด schema) - เติมให้ถูก
    zoom = doc.settings.element.find(qn("w:zoom"))
    if zoom is not None and zoom.get(qn("w:percent")) is None:
        zoom.set(qn("w:percent"), "100")
    return doc


def add_page_number(section) -> None:
    paragraph = section.footer.paragraphs[0]
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    run = paragraph.add_run()
    for kind, text in (("begin", None), (None, "PAGE"), ("end", None)):
        if kind:
            element = OxmlElement("w:fldChar")
            element.set(qn("w:fldCharType"), kind)
        else:
            element = OxmlElement("w:instrText")
            element.set(qn("xml:space"), "preserve")
            element.text = text
        run._element.append(element)
    style_run(run, 13)


def para(doc, text: str = "", size: float = BODY_PT, bold: bool = False, align=None, space_after: float = 3):
    paragraph = doc.add_paragraph()
    if align is not None:
        paragraph.alignment = align
    paragraph.paragraph_format.space_after = Pt(space_after)
    add_rich(paragraph, text, size, bold)
    return paragraph


def add_rich(paragraph, text: str, size: float = BODY_PT, bold: bool = False) -> None:
    """ข้อความที่มี **ตัวหนา** แบบง่าย ๆ"""
    for index, chunk in enumerate(text.split("**")):
        if chunk:
            style_run(paragraph.add_run(chunk), size, bold=bold or index % 2 == 1)


def bullets(doc, items: list[str], numbered: bool = False, size: float = BODY_PT) -> None:
    for item in items:
        paragraph = doc.add_paragraph(style="List Number" if numbered else "List Bullet")
        paragraph.paragraph_format.space_after = Pt(1)
        add_rich(paragraph, item, size)


def heading(doc, text: str, level: int = 1) -> None:
    paragraph = doc.add_heading(level=level)
    style_run(paragraph.add_run(text), 19 if level == 1 else 16.5, bold=True, color=ACCENT)


def figure(doc, path: Path, width_cm: float, caption: str) -> None:
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(0)
    paragraph.paragraph_format.keep_with_next = True
    paragraph.add_run().add_picture(str(path), width=Cm(width_cm))
    cap = doc.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    cap.paragraph_format.space_after = Pt(6)
    add_rich(cap, caption, 13.5)


def shade(cell, hex_fill: str) -> None:
    props = cell._element.get_or_add_tcPr()
    fill = OxmlElement("w:shd")
    fill.set(qn("w:val"), "clear")
    fill.set(qn("w:color"), "auto")
    fill.set(qn("w:fill"), hex_fill)
    props.append(fill)


def set_cell(cell, text: str, size: float = 13.5, bold: bool = False, color: RGBColor | None = None, align=None) -> None:
    cell.text = ""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        paragraph = cell.paragraphs[0] if i == 0 else cell.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(0)
        if align is not None:
            paragraph.alignment = align
        for index, chunk in enumerate(line.split("**")):
            if chunk:
                style_run(paragraph.add_run(chunk), size, bold=bold or index % 2 == 1, color=color)


def table(doc, header: list[str], rows: list[list[str]], widths_cm: list[float], size: float = 13.5):
    grid = doc.add_table(rows=1, cols=len(header))
    grid.style = "Table Grid"
    grid.alignment = WD_TABLE_ALIGNMENT.CENTER
    for i, text in enumerate(header):
        set_cell(grid.rows[0].cells[i], text, size, bold=True)
        shade(grid.rows[0].cells[i], "DCE6F0")
    for row in rows:
        cells = grid.add_row().cells
        for i, text in enumerate(row):
            set_cell(cells[i], text, size)
    for index, row in enumerate(grid.rows):
        for i, width in enumerate(widths_cm):
            row.cells[i].width = Cm(width)
        # ห้ามแถวถูกตัดครึ่งข้ามหน้า และให้หัวตารางซ้ำเมื่อตารางยาวข้ามหน้า
        props = row._tr.get_or_add_trPr()
        props.append(OxmlElement("w:cantSplit"))
        if index == 0:
            props.append(OxmlElement("w:tblHeader"))
    doc.add_paragraph().paragraph_format.space_after = Pt(2)
    return grid


def title_block(doc, title: str, subtitle: str) -> None:
    paragraph = doc.add_paragraph()
    paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
    paragraph.paragraph_format.space_after = Pt(0)
    style_run(paragraph.add_run(title), 23, bold=True, color=ACCENT)
    sub = doc.add_paragraph()
    sub.alignment = WD_ALIGN_PARAGRAPH.CENTER
    sub.paragraph_format.space_after = Pt(4)
    style_run(sub.add_run(subtitle), 16)

    fields = [
        ("รายวิชา", "[กรอกรหัสและชื่อรายวิชา]"),
        ("ชื่อกลุ่ม", "[กรอกชื่อกลุ่ม]"),
        ("สมาชิก", "[ชื่อ–สกุล]  [รหัสนักศึกษา]\n[ชื่อ–สกุล]  [รหัสนักศึกษา]\n[ชื่อ–สกุล]  [รหัสนักศึกษา]"),
        ("อาจารย์ผู้สอน", "[กรอกชื่ออาจารย์]"),
    ]
    grid = doc.add_table(rows=0, cols=2)
    grid.style = "Table Grid"
    grid.alignment = WD_TABLE_ALIGNMENT.CENTER
    for label, value in fields:
        cells = grid.add_row().cells
        set_cell(cells[0], label, 14, bold=True)
        shade(cells[0], "EEF2F6")
        set_cell(cells[1], value, 14, color=PLACEHOLDER)
    for row in grid.rows:
        row.cells[0].width = Cm(3.4)
        row.cells[1].width = Cm(13.2)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def side_by_side(doc, image: Path, image_cm: float, items: list[str], caption: str) -> None:
    grid = doc.add_table(rows=1, cols=2)
    grid.alignment = WD_TABLE_ALIGNMENT.CENTER
    left, right = grid.rows[0].cells
    left.width, right.width = Cm(image_cm + 0.4), Cm(16.6 - image_cm - 0.4)
    picture = left.paragraphs[0]
    picture.alignment = WD_ALIGN_PARAGRAPH.CENTER
    picture.add_run().add_picture(str(image), width=Cm(image_cm))
    cap = left.add_paragraph()
    cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
    add_rich(cap, caption, 13)
    right.text = ""
    for i, item in enumerate(items):
        paragraph = right.paragraphs[0] if i == 0 else right.add_paragraph()
        paragraph.paragraph_format.space_after = Pt(4)
        add_rich(paragraph, item, 14.5)
    doc.add_paragraph().paragraph_format.space_after = Pt(2)


def compass(bearing: float | None) -> str:
    if bearing is None:
        return "ไม่ชัดเจน"
    names = ["เหนือ", "ตะวันออกเฉียงเหนือ", "ตะวันออก", "ตะวันออกเฉียงใต้",
             "ใต้", "ตะวันตกเฉียงใต้", "ตะวันตก", "ตะวันตกเฉียงเหนือ"]
    return names[int((bearing % 360 + 22.5) // 45) % 8]


def km(value) -> str:
    return "—" if value is None else f"{value:.1f} กม."


def live_sources_text(live: dict) -> str:
    """ผลทดสอบรับข้อมูลสดจาก Blitzortung (ค่าจาก docs/figures/live_blitzortung.json)"""
    nearest = (
        f"ฟ้าผ่าที่ใกล้สนามที่สุดห่าง {live['nearest_km']:.1f} กม. ({live['nearest_stadium']})"
        if live.get("nearest_km") is not None else "ยังไม่มีฟ้าผ่าเข้าใกล้สนาม"
    )
    return (
        "**ทำไมใช้สองแหล่ง:** ไม่มีข้อมูลฟ้าผ่าฟรีแหล่งเดียวที่ครบในไทย Blitzortung ฟรีและส่งข้อมูลสดตลอด "
        "แต่สถานีในภูมิภาคมีน้อย จับได้เฉพาะฟ้าผ่าที่แรง ส่วน Xweather ครบกว่าแต่มี quota "
        "ระบบจึงรันทั้งสองพร้อมกันและตัดข้อมูลซ้ำ ผลทดสอบรับข้อมูลสด "
        f"{live['window']}: ได้ฟ้าผ่าทั่วโลก {live['received_total']:,} จุด (ราว {live['per_minute']:,.0f} จุด/นาที) "
        f"อยู่ในกรอบประเทศไทย {live['in_region_total']:,} จุด, {nearest} "
        f"และบันทึกลงฐานข้อมูล {live['stored_total']:,} จุด"
    )


# ============================================================ System Design


def system_design(stats: dict) -> Path:
    doc = new_document()
    title_block(
        doc,
        "System Design",
        "ระบบแจ้งเตือนฟ้าผ่าล่วงหน้าสำหรับสนามฟุตบอลไทยลีก 1 (GeoAI for Disaster)",
    )

    heading(doc, "1. ภาพรวมระบบ")
    para(doc,
         f"ระบบเฝ้าระวังความเสี่ยงฟ้าผ่ารอบสนามฟุตบอลไทยลีก 1 ฤดูกาล 2026–27 จำนวน {stats['stadiums_active']} สนาม "
         "โดยรวมข้อมูลจริง 2 ประเภท คือ **ตำแหน่งฟ้าผ่าสด** (Blitzortung ฟรี และ Xweather เมื่อมี quota) "
         "และ **ภาพเรดาร์ฝน** (RainViewer) "
         "แล้วใช้ GeoAI แบบง่ายตัดสินสถานะความปลอดภัยของแต่ละสนามตามเกณฑ์ NCAA Lightning Safety "
         "พร้อมพยากรณ์ว่าพายุจะถึงสนามในอีกกี่นาที และแสดงผลบนแผนที่เชิงโต้ตอบ "
         "ระบบใช้ข้อมูลจริงเท่านั้น ไม่มีข้อมูลจำลอง")

    heading(doc, "2. สถาปัตยกรรม Frontend–Backend–Database–Model")
    figure(doc, FIG / "diagram_architecture.png", 15.0,
           "**รูปที่ 1** สถาปัตยกรรมของระบบ แบ่งเป็น 4 ชั้นตามหน้าที่")
    table(
        doc,
        ["ชั้น", "เทคโนโลยี", "หน้าที่หลัก", "ไฟล์หลัก"],
        [
            ["**Frontend**", "Leaflet, HTML/CSS/JS", "แผนที่เชิงโต้ตอบ สีสถานะสนาม วงรัศมี 10/13/16 กม. "
             "ไอคอนสายฟ้า ชั้นภาพเรดาร์ และโหมด replay พายุจริง", "static/"],
            ["**Backend**", "FastAPI (Python)", "รับข้อมูล (Blitzortung แบบ stream, Xweather ทุก 60 วินาที, RainViewer ทุก 5 นาที) "
             "ตัดฟ้าผ่าซ้ำข้ามแหล่ง กรองรอบสนาม และให้บริการ REST API 10 endpoint", "app/main.py\napp/sources/"],
            ["**Database**", "SQLite", "เก็บข้อมูลสนาม ฟ้าผ่า และผลวิเคราะห์เรดาร์ 5 ตาราง "
             "ล้างข้อมูลเก่าอัตโนมัติ", "app/db.py"],
            ["**Model**", "Python (GeoAI)", "หา hotspot/cell ฝนแรงจากเรดาร์, จัดระดับตามเกณฑ์ NCAA, "
             "nowcast การเคลื่อนที่, รวมหลักฐาน (fusion)", "app/model/"],
        ],
        [2.4, 3.2, 8.2, 2.8],
    )

    heading(doc, "3. Data Flow")
    figure(doc, FIG / "diagram_dataflow.png", 16.6,
           "**รูปที่ 2** เส้นทางข้อมูลตั้งแต่ข้อมูลเข้าจนผู้ใช้เห็นผลบนแผนที่")
    bullets(doc, [
        "**ข้อมูลเข้า:** ฟ้าผ่าสดจาก Blitzortung (WebSocket ฟรี) และ Xweather API (เมื่อมี quota) "
        "กับภาพเรดาร์ฝนจาก RainViewer "
        "(ฝั่ง server ดึงข้อมูลเองทั้งหมด เบราว์เซอร์ไม่เคยเรียก API ภายนอกโดยตรง)",
        "**ประมวลผล:** ตัดฟ้าผ่าลูกเดียวกันที่สองเครือข่ายรายงานซ้ำ (≤ 1 วินาที, ≤ 5 กม.) "
        "กรองเก็บเฉพาะที่ห่างสนามไม่เกิน 30 กม. (สูตร haversine) "
        "และแปลงสีภาพเรดาร์เป็นค่า dBZ แล้วหากลุ่มฝนแรง (cell) ที่ ≥ 40 dBZ",
        "**จัดเก็บ:** บันทึกลง SQLite พร้อมเวลาที่เกิดจริงและแหล่งที่มาของข้อมูลทุกแถว",
        "**วิเคราะห์ต่อคำขอ:** เมื่อหน้าเว็บขอข้อมูล ระบบจัดระดับสถานะ (tiering) "
        "พยากรณ์ ETA (nowcast) และรวมหลักฐาน (fusion) จากข้อมูลล่าสุด",
        "**API → ผู้ใช้:** ส่ง JSON ให้หน้าเว็บทุก 30 วินาที ผู้ใช้เห็นสีสถานะ ระยะฟ้าผ่า "
        "เวลาที่คาดว่าพายุจะถึง และนับถอยหลังก่อนกลับมาเล่นได้",
    ], size=14.5)

    heading(doc, "4. ตรรกะตัดสินสถานะ (Fusion)")
    side_by_side(
        doc, FIG / "diagram_fusion.png", 7.6,
        [
            "**ฟ้าผ่าเท่านั้นที่สั่งหยุดแข่งได้** ใช้เกณฑ์ NCAA: ฟ้าผ่าในรัศมี 13 กม. (≈ 8 ไมล์) "
            "ภายใน 30 นาที = สั่งหยุด และรัศมี 10 กม. = อันตราย",
            "**เรดาร์ใช้เตือนล่วงหน้า** ฝน ≥ 40 dBZ ในรัศมี 16 กม. ทำให้ขึ้นเฝ้าระวังได้ "
            "แม้ยังไม่พบฟ้าผ่า แต่สั่งหยุดแข่งไม่ได้ เพราะฝนหนักไม่ได้แปลว่ามีฟ้าผ่าเสมอ",
            "**ไม่มีข้อมูล ≠ ปลอดภัย** ถ้าแหล่งข้อมูลฟ้าผ่าใช้งานไม่ได้ (เช่น quota หมด) "
            "และไม่มีหลักฐานอื่น ระบบแสดง \"ไม่มีข้อมูล\" แทนการแสดงว่าปลอดภัย",
            "**เครือข่ายฟรีจับได้ไม่ครบ** สถานี Blitzortung ในไทยมีน้อย ถ้ามีแต่ข้อมูลจากเครือข่ายนี้ "
            "จะขึ้นปลอดภัยได้เมื่อเรดาร์ยืนยันด้วยว่าไม่มีฝนแรง ถ้าเรดาร์ไม่พร้อมจะแสดง \"ไม่มีข้อมูล\"",
            "**กลับมาเล่นได้** เมื่อไม่มีฟ้าผ่าในรัศมี 13 กม. ครบ 30 นาที "
            "และไม่มีฝนแรงในรัศมี 16 กม.",
        ],
        "**รูปที่ 3** ผังตัดสินสถานะของสนาม",
    )

    target = OUT / "1_System_Design.docx"
    doc.save(target)
    return target


# ================================================================== Report


def report(stats: dict) -> Path:
    storm, radar, tests = stats["storm"], stats["radar"], stats["tests"]
    doc = new_document()
    title_block(
        doc,
        "รายงานผล MVP Prototype",
        "ระบบแจ้งเตือนฟ้าผ่าล่วงหน้าสำหรับสนามฟุตบอลไทยลีก 1 (GeoAI for Disaster)",
    )

    # ---------------------------------------------------------------- 1
    heading(doc, "1. MVP ของกลุ่มคืออะไร")
    para(doc,
         "ฟ้าผ่าเป็นภัยพิบัติที่อันตรายต่อผู้เล่นและผู้ชมในสนามกีฬากลางแจ้ง โดยเฉพาะช่วงฤดูฝนของไทย "
         "ซึ่งมีพายุฝนฟ้าคะนองเกือบทุกวัน ผู้จัดการแข่งขันต้องตัดสินใจให้ทันว่าควรหยุดเกมเมื่อใด "
         "และกลับมาแข่งต่อได้เมื่อใด MVP นี้จึงเป็น **ระบบแจ้งเตือนฟ้าผ่าล่วงหน้า** "
         f"สำหรับสนามไทยลีก 1 ทั้ง {stats['stadiums_active']} สนาม มีความสามารถหลักดังนี้")
    bullets(doc, [
        "ติดตามฟ้าผ่าจริงรอบทุกสนาม และวิเคราะห์ภาพเรดาร์ฝนจริงเพื่อหากลุ่มฝนแรงที่อาจก่อฟ้าผ่า",
        "จัดสถานะแต่ละสนามเป็น ปลอดภัย / เฝ้าระวัง / สั่งหยุด / อันตราย ตามเกณฑ์ NCAA "
        "และแสดง \"ไม่มีข้อมูล\" เมื่อแหล่งข้อมูลใช้งานไม่ได้",
        "พยากรณ์ระยะสั้น (nowcast) ว่าพายุจะถึงสนามในอีกกี่นาที และนับถอยหลังก่อนกลับมาแข่งได้",
        "แสดงผลบนแผนที่เชิงโต้ตอบ และเล่นย้อนเหตุการณ์พายุจริงที่บันทึกไว้ (replay) ได้",
    ])
    para(doc, "ความสอดคล้องกับข้อกำหนดของ MVP Prototype:", bold=True)
    table(
        doc,
        ["ข้อกำหนด", "สิ่งที่กลุ่มทำ"],
        [
            ["Frontend: แผนที่เชิงโต้ตอบ", "Leaflet แสดงสถานะสนาม วงรัศมี ไอคอนสายฟ้า ภาพเรดาร์ และโหมด replay"],
            ["Backend: API ≥ 2 endpoint", "FastAPI 10 endpoint เช่น /api/risk, /api/strikes/recent, /api/radar, /api/replays"],
            ["Database: เก็บข้อมูล hazard", "SQLite 5 ตาราง: สนาม ฟ้าผ่า ภาพเรดาร์ cell ฝน และระยะฝนถึงสนาม"],
            ["Model/Rule: GeoAI ง่าย ๆ", "hotspot count จากเรดาร์ (≥ 40 dBZ → cell), กฎ NCAA ตามระยะ, "
             "nowcast ด้วย least squares และ fusion"],
        ],
        [5.0, 11.6],
    )

    # ---------------------------------------------------------------- 2
    heading(doc, "2. MVP ทำงานอย่างไร")
    heading(doc, "2.1 ข้อมูลจริงที่ใช้", 2)
    table(
        doc,
        ["แหล่งข้อมูล", "ข้อมูล", "ความถี่", "หมายเหตุ"],
        [
            ["Blitzortung.org (ฟรี)", "ตำแหน่ง เวลา ขั้วประจุของฟ้าผ่า", "สด (stream)",
             "ไม่มี quota แต่สถานีในไทยน้อย จับได้ไม่ครบ"],
            ["Xweather Lightning API", "ตำแหน่ง เวลา ขั้วประจุของฟ้าผ่า", "ทุก 60 วินาที",
             "แม่นกว่า มี quota รายเดือน (หมดถึง ต.ค.)"],
            ["RainViewer", "ภาพเรดาร์ฝน (zoom 7 ≈ 1.2 กม./pixel)", "ภาพใหม่ทุก 10 นาที",
             "ใช้เพื่อการศึกษา ต้องให้เครดิต"],
            ["GeoJSON สนามไทยลีก 1", "พิกัดและความจุ 17 สนาม\n(ใช้งานจริง 16 สนาม)", "โหลดครั้งเดียว",
             "พิกัด 1 สนามยังรอตรวจสอบ"],
        ],
        [3.6, 5.2, 2.8, 5.0],
    )
    para(doc, live_sources_text(stats["live"]), size=14.5)

    heading(doc, "2.2 GeoAI และกฎตัดสินใจ", 2)
    bullets(doc, [
        "**Hotspot และ cell จากเรดาร์:** แปลงสีแต่ละ pixel เป็นค่า dBZ ด้วยตารางสีทางการ "
        "นับ pixel ที่ ≥ 40 dBZ เป็น hotspot แล้วจัดกลุ่ม hotspot ที่ติดกัน (8 ทิศ) เป็น cell "
        "ตัดกลุ่มที่เล็กกว่า 4 pixel ออก จากนั้นวัดระยะจาก cell ถึงแต่ละสนาม",
        "**กฎ NCAA (tiering):** ฟ้าผ่าภายใน 30 นาทีที่ ≤ 10 กม. = อันตราย, ≤ 13 กม. = สั่งหยุด, "
        "≤ 16 กม. = เฝ้าระวัง และกลับมาเล่นได้เมื่อไม่มีฟ้าผ่าในรัศมี 13 กม. ครบ 30 นาที",
        "**Nowcast:** แบ่งฟ้าผ่า 20 นาทีล่าสุดเป็นช่วงละ 5 นาที หาจุดศูนย์กลางแต่ละช่วง "
        "ใช้ least squares หาเวกเตอร์ความเร็ว แล้วคำนวณเวลาที่พายุจะถึงวง 13 กม. "
        "ถ้าข้อมูลน้อยหรือทิศไม่ชัด ระบบจะตอบว่า \"ไม่รู้\" แทนการเดา",
        "**Fusion:** ฟ้าผ่าเป็นหลักฐานสั่งหยุด เรดาร์เป็นสัญญาณเตือนล่วงหน้า "
        "ถ้าข้อมูลฟ้าผ่าไม่พร้อม ระบบจะไม่แสดงว่าปลอดภัย และถ้ามีแต่ข้อมูลจาก Blitzortung "
        "ต้องมีเรดาร์ยืนยันว่าไม่มีฝนแรงก่อนจึงขึ้นปลอดภัย",
    ], size=14.5)

    heading(doc, "2.3 การใช้งานหน้าเว็บ", 2)
    figure(doc, FIG / "screen_live_radar.png", 15.2,
           "**รูปที่ 1** หน้าเว็บโหมดสด 17/09/2026 เวลา 17:19 น.: รับฟ้าผ่าสดจาก Blitzortung "
           "(Xweather ติด quota ระบบข้ามไปเอง) พร้อมภาพเรดาร์จริง วงกลมคือ cell ฝนแรงที่ระบบตรวจพบ "
           f"สนาม {stats['live']['summary']['WATCH']} แห่งขึ้นเฝ้าระวังจากฝนแรงใกล้สนาม "
           f"และ {stats['live']['summary']['ALL_CLEAR']} แห่งปลอดภัยหลังเรดาร์ยืนยันว่าไม่มีฝนแรง")

    # ---------------------------------------------------------------- 3
    heading(doc, "3. ผลลัพธ์")
    heading(doc, f"3.1 เหตุการณ์พายุจริง {storm['date_thai']} สนาม {storm['stadium']} ({storm['club']})", 2)
    transitions = storm["transitions"][1:]
    suspend_minutes = storm["minutes_by_status"].get("SUSPEND", 0) + storm["minutes_by_status"].get("DANGER", 0)
    para(doc,
         f"ระบบบันทึกฟ้าผ่าจริงจาก Xweather ได้ {storm['strikes_total']} ครั้ง ระหว่าง {storm['first_strike']}–"
         f"{storm['last_strike']} น. ในจำนวนนี้ {storm['strikes_near_stadium']} ครั้งอยู่ในรัศมี 30 กม. "
         f"ของสนาม {storm['stadium']} ครั้งที่ใกล้ที่สุดห่างเพียง **{storm['closest_km']:.1f} กม.** "
         f"เวลา {storm['closest_time']} น. เมื่อเล่นย้อนเหตุการณ์ในระบบ สถานะของสนามเปลี่ยนตามตารางด้านล่าง "
         f"รวมเวลาที่อยู่ในสถานะสั่งหยุดหรืออันตราย {suspend_minutes} นาที")
    table(
        doc,
        ["เวลา (น.)", "สถานะ", "ฟ้าผ่าใกล้สุดใน 30 นาที", "จำนวนใน 30 นาที", "ความเร็วพายุ (nowcast)"],
        [
            [t["time"], STATUS_THAI[t["status"]], km(t["closest_km"]), str(t["strike_count"] or 0),
             f"{t['speed_kmph']:.0f} กม./ชม." if t["speed_kmph"] else "—"]
            for t in transitions
        ],
        [2.2, 4.0, 3.9, 2.9, 3.6],
    )
    figure(doc, FIG / "screen_replay_danger.png", 15.2,
           f"**รูปที่ 2** โหมด replay เวลา 13:52 น.: สนาม {storm['stadium']} ขึ้นสถานะอันตราย "
           "ไอคอนสายฟ้าที่กะพริบคือฟ้าผ่าภายใน 5 นาที")
    figure(doc, FIG / "storm_timeline.png", 15.6,
           "**รูปที่ 3** ระยะของฟ้าผ่าแต่ละครั้งจากสนาม เทียบกับสถานะที่ระบบตัดสิน "
           "(เส้นประ = รัศมี 10/13/16 กม.)")
    side_by_side(
        doc, FIG / "storm_map.png", 8.6,
        [
            "**สิ่งที่เห็นจากผล**",
            f"ฟ้าผ่าเริ่มทางทิศใต้ของสนาม แล้วเคลื่อนผ่านด้านตะวันออก nowcast ประมาณทิศการเคลื่อนที่ไปทาง"
            f"**{compass(storm['nowcast_bearing_busiest'])}** ด้วยความเร็วราว "
            f"{storm['nowcast_speed_busiest']:.0f} กม./ชม. (ค่ามัธยฐานทั้งเหตุการณ์ "
            f"{storm['nowcast_speed_median']:.0f} กม./ชม.) สอดคล้องกับความเร็วปกติของพายุฝนฟ้าคะนอง",
            "สถานะเปลี่ยนตามกฎ NCAA ถูกต้อง: ฟ้าผ่าครั้งแรกที่จับได้อยู่ในวง 13 กม. "
            "จึงสั่งหยุดทันทีโดยไม่ผ่านเฝ้าระวัง และกลับเป็นปลอดภัยพอดี 30 นาทีหลังฟ้าผ่าครั้งสุดท้ายในวง 13 กม.",
        ],
        "**รูปที่ 4** ตำแหน่งฟ้าผ่ารอบสนาม ไล่สีตามเวลา",
    )

    heading(doc, "3.2 ผลวิเคราะห์ภาพเรดาร์จริง", 2)
    figure(doc, FIG / "radar_summary.png", 15.6,
           f"**รูปที่ 5** ผลวิเคราะห์ภาพเรดาร์จริง {radar['frames']} ภาพ ({radar['first_frame']}–"
           f"{radar['last_frame'].split(' ')[-1]} น.)")
    para(doc,
         f"ระบบแปลงสีภาพเรดาร์เป็นค่า dBZ ได้ตรงตารางสีทางการ 100% และตรวจพบ cell ฝนแรงรวม "
         f"{radar['cells_total']:,} cell (มากสุด {radar['cells_max_per_frame']} cell ต่อภาพ) ค่าสะท้อนสูงสุด "
         f"{radar['max_dbz']} dBZ ฝนแรงที่เข้าใกล้สนามที่สุดอยู่ห่าง {radar['closest_hotspot_stadium']} "
         f"{radar['closest_hotspot_km']:.1f} กม. เวลา {radar['closest_hotspot_time']} น. "
         + (
             f"ตลอดช่วงนี้ฝนแรงเข้าวงเฝ้าระวัง 16 กม. รวม {radar['watch_exposures']} ครั้ง "
             f"ใน {radar['watch_stadiums']} สนาม ระบบยกสนามเหล่านั้นขึ้นเฝ้าระวัง แต่ไม่สั่งหยุด "
             "เพราะเรดาร์อย่างเดียวไม่ใช่หลักฐานฟ้าผ่า"
             if radar["watch_exposures"] else
             "ซึ่งยังอยู่นอกวงเฝ้าระวัง 16 กม. ระบบจึงไม่ยกระดับสถานะ"
         ))

    heading(doc, "3.3 การทดสอบ", 2)
    para(doc,
         f"ชุดทดสอบอัตโนมัติ {tests['total']} ข้อผ่านทั้งหมด ครอบคลุมกฎ NCAA และนาฬิกา all-clear, nowcast, "
         "การวิเคราะห์เรดาร์ การรับข้อมูล การจัดการกรณีข้อมูลไม่พร้อม และโหมด replay "
         "นอกจากนี้ได้ทดสอบกับข้อมูลจริง เช่น ระยะที่ Xweather คำนวณตรงกับสูตร haversine ของระบบ "
         "คลาดเคลื่อนไม่เกิน 1 เมตร เมื่อ quota หมดระบบหยุดเรียก API ทันที และเมื่อรันจริงกับ Blitzortung "
         "ขณะเรดาร์ยังโหลดภาพไม่ทัน สนามแสดง \"ไม่มีข้อมูล\" แทน \"ปลอดภัย\" ตามกฎที่ออกแบบไว้", size=14.5)

    # ---------------------------------------------------------------- 4
    heading(doc, "4. ข้อจำกัด")
    bullets(doc, [
        "**ข้อมูลฟ้าผ่าฟรีจับได้ไม่ครบ:** Blitzortung มีสถานีในไทยน้อย ฟ้าผ่าที่อ่อนอาจไม่ปรากฏ "
        "จึงอาจสั่งหยุดช้ากว่าความจริง ระบบชดเชยด้วยเรดาร์ (เฝ้าระวัง) และกฎยืนยันด้วยเรดาร์ก่อนขึ้นปลอดภัย "
        "ส่วน quota ของ Xweather หมดระหว่างพัฒนา ใช้เสริมได้อีกครั้งรอบเดือนตุลาคม",
        "**ข้อมูลเหตุการณ์ยังน้อย:** ยืนยันผลกับพายุจริงเพียง 1 เหตุการณ์ จึงยังวัดอัตราการเตือนผิด "
        "หรือเวลาเตือนล่วงหน้าเชิงสถิติไม่ได้ และเหตุการณ์นี้บันทึกก่อนเพิ่มโมดูลเรดาร์ "
        "จึงยังไม่มีเหตุการณ์ที่มีทั้งฟ้าผ่าและเรดาร์พร้อมกันให้เทียบว่าเรดาร์เตือนก่อนฟ้าผ่ากี่นาที",
        "**เรดาร์ไม่ใช่ฟ้าผ่า:** ภาพเรดาร์เป็นค่าสะท้อนรวมทุกความสูง บอกได้ว่ามีฝนแรง แต่บอกไม่ได้ว่ามีประจุไฟฟ้า "
        "เกณฑ์ 40 dBZ เป็นค่าประมาณ ความละเอียด ~1.2 กม. และมีภาพทุก 10 นาที",
        "**Nowcast แบบเส้นตรง:** สมมติพายุเคลื่อนที่ตรงด้วยความเร็วคงที่ เมื่อข้อมูลน้อยอาจประมาณผิด "
        f"(พบค่าที่เป็นไปไม่ได้ถึง 349 กม./ชม. จึงเพิ่มเกณฑ์ไม่แสดงค่าที่เกิน 120 กม./ชม.)",
        "**Replay:** ไฟล์บันทึกไม่ได้เก็บช่วงที่เซิร์ฟเวอร์หยุดทำงาน (มีช่องว่างราว 2 นาทีในเหตุการณ์ 16 ก.ย.)",
        "**ข้อมูลสนาม:** พิกัดสนาม Rasisalai United มาจากการ geocode ยังไม่ได้ตรวจสอบ (Chonburi ตรวจและแก้แล้ว)",
        "**ขอบเขตการใช้งาน:** เป็นต้นแบบเชิงวิชาการ ไม่ใช่ระบบสำหรับตัดสินใจด้านความปลอดภัยจริง "
        "ข้อมูล RainViewer ใช้ได้เฉพาะงานการศึกษา และ Blitzortung อนุญาตเฉพาะงานไม่เชิงพาณิชย์ "
        "ห้ามใช้ตัดสินใจด้านความปลอดภัยจริง",
    ], size=14.5)

    heading(doc, "5. สรุปและแนวทางพัฒนาต่อ")
    para(doc,
         "MVP นี้ทำงานได้ครบตั้งแต่รับข้อมูลจริง ประมวลผลด้วย GeoAI จัดเก็บ ให้บริการผ่าน API "
         "จนแสดงผลบนแผนที่ และให้ผลที่สอดคล้องกับเกณฑ์ NCAA เมื่อทดสอบกับพายุจริง แนวทางพัฒนาต่อ ได้แก่", size=14.5)
    bullets(doc, [
        "บันทึกเหตุการณ์ที่มีทั้งฟ้าผ่าและเรดาร์เพิ่ม เพื่อวัดเวลาเตือนล่วงหน้าและอัตราการเตือนผิด แล้วใช้ปรับเกณฑ์ 40 dBZ",
        "ส่งการแจ้งเตือนถึงเจ้าหน้าที่สนามโดยตรง",
        "ทดลองแบบจำลองที่เรียนรู้จากข้อมูลที่สะสมทั้งฤดูกาลแทนสมการเส้นตรง",
    ], numbered=True, size=14.5)

    target = OUT / "3_Report.docx"
    doc.save(target)
    return target


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    stats = json.loads((FIG / "stats.json").read_text(encoding="utf-8"))
    stats["live"] = json.loads((FIG / "live_blitzortung.json").read_text(encoding="utf-8"))
    for path in (system_design(stats), report(stats)):
        print(f"  ok  {path.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
