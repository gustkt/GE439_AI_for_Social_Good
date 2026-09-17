"""สร้าง diagram (mermaid) และภาพหน้าจอเว็บสำหรับเอกสาร ด้วย Microsoft Edge แบบ headless

รัน:  python -m docs.build.make_visuals      (ต้องเปิดเซิร์ฟเวอร์ที่ http://127.0.0.1:8000 ไว้ก่อน)

ผลลัพธ์ (docs/figures/):
  diagram_architecture.png   สถาปัตยกรรม Frontend–Backend–Database–Model
  diagram_dataflow.png       Data flow: ข้อมูลเข้า → ประมวลผล → จัดเก็บ → API → ผู้ใช้
  diagram_fusion.png         ตรรกะตัดสินสถานะ (fusion)
  screen_replay_danger.png   หน้าเว็บ: replay พายุจริง 16 ก.ย. ช่วงสนามขึ้น "อันตราย"
  screen_live_radar.png      หน้าเว็บ: โหมดสดพร้อมเรดาร์และ cell ฝนแรง

diagram ใช้ mermaid จาก CDN จึงต้องต่ออินเทอร์เน็ต ภาพหน้าจอเป็นข้อมูลจริงจากระบบที่รันอยู่
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import urllib.request
from pathlib import Path

from PIL import Image, ImageChops

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "figures"
EDGE = Path(os.environ.get("EDGE_PATH", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"))
SERVER = "http://127.0.0.1:8000"

# ให้ mermaid จัดวางที่ขนาดปกติ (วัดตัวอักษรถูกต้อง) แล้วค่อยขยาย SVG ที่วาดเสร็จแล้ว
# ห้ามใช้ CSS zoom ก่อนวาด - ทำให้ mermaid วัดขนาดข้อความผิดจนกล่องและข้อความเพี้ยน
MERMAID_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<style>
  body {{ margin: 0; padding: 30px; background: #ffffff; }}
  #out svg {{ max-width: none !important; }}
</style>
<script src="https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.min.js"></script>
</head><body>
<div id="out"></div>
<script>
  mermaid.initialize({{
    startOnLoad: false,
    theme: 'base',
    flowchart: {{ curve: 'basis', padding: 12, nodeSpacing: 38, rankSpacing: 52 }},
    themeVariables: {{
      fontFamily: 'TH Sarabun New, Tahoma, sans-serif', fontSize: '24px',
      primaryColor: '#eaf3fb', primaryBorderColor: '#4a7fb0', primaryTextColor: '#10263b',
      lineColor: '#4a6378', clusterBkg: '#f6f8fa', clusterBorder: '#9fb3c8',
      edgeLabelBackground: '#ffffff'
    }}
  }});
  (async () => {{
    const {{ svg }} = await mermaid.render('diagram', {code});
    const out = document.getElementById('out');
    out.innerHTML = svg;
    const el = out.querySelector('svg');
    const box = el.viewBox.baseVal;
    // ขยายให้พอดีกรอบหน้าต่าง (เว้นขอบ) โดยรักษาสัดส่วน - SVG เป็นเวกเตอร์จึงคมทุกขนาด
    const fit = Math.min(({width} - 90) / box.width, ({height} - 90) / box.height);
    el.setAttribute('width', box.width * fit);
    el.setAttribute('height', box.height * fit);
  }})();
</script>
</body></html>"""

ARCHITECTURE = """
flowchart LR
    EXT["<b>แหล่งข้อมูลจริง</b><br/>Blitzortung — ฟ้าผ่าสด (ฟรี)<br/>Xweather — ฟ้าผ่า (มี quota)<br/>RainViewer — ภาพเรดาร์<br/>ไฟล์ capture — replay"]
    BE["<b>Backend</b><br/>FastAPI (Python)<br/>รวมแหล่ง + ตัดฟ้าผ่าซ้ำ<br/>ingest กรองรัศมี 30 กม.<br/>REST API 10 endpoint"]
    DB[("<b>Database</b><br/>SQLite<br/>stadiums · strikes<br/>radar_frames · radar_cells<br/>radar_exposure")]
    MD["<b>Model (GeoAI)</b><br/>radar_cells: dBZ → hotspot → cell<br/>tiering: เกณฑ์ NCAA 10/13/16 กม.<br/>nowcast: least squares → ETA<br/>fusion: รวมหลักฐาน → สถานะ"]
    FE["<b>Frontend</b><br/>Leaflet (HTML / JS)<br/>แผนที่ + สีสถานะสนาม<br/>สายฟ้า · เรดาร์ · วงรัศมี<br/>โหมด replay"]

    EXT -->|ข้อมูลดิบ| BE
    BE -->|บันทึก| DB
    DB -->|strike + ผลเรดาร์| MD
    MD -->|สถานะ + ETA| BE
    BE -->|JSON| FE

    style BE fill:#eaf3fb,stroke:#4a7fb0
    style DB fill:#eef7ee,stroke:#4a9a5a
    style MD fill:#f4ecf7,stroke:#8e5aa8
    style FE fill:#fdf2e3,stroke:#d08a2e
    style EXT fill:#f5f5f5,stroke:#8a8a8a
"""

DATAFLOW = """
flowchart LR
    subgraph S1["① ข้อมูลเข้า"]
        direction TB
        A1["ฟ้าผ่า: Blitzortung stream สด<br/>+ Xweather (เมื่อมี quota)"]
        A2["RainViewer เรดาร์<br/>ภาพทุก 10 นาที"]
    end
    subgraph S2["② ประมวลผล"]
        direction TB
        B1["ตัดฟ้าผ่าซ้ำข้ามแหล่ง<br/>กรอง strike ≤ 30 กม. (haversine)"]
        B2["สีภาพ → dBZ<br/>hotspot ≥ 40 dBZ → cell"]
    end
    C1[("③ จัดเก็บ<br/>SQLite")]
    B3["④ วิเคราะห์ต่อคำขอ<br/>tiering + nowcast + fusion"]
    D1["⑤ REST API<br/>JSON"]
    E1["⑥ ผู้ใช้เห็นผลบนแผนที่<br/>สีสถานะ · วงรัศมี · สายฟ้า<br/>cell ฝน · ETA · all-clear"]

    A1 --> B1 --> C1
    A2 --> B2 --> C1
    C1 --> B3 --> D1 --> E1
"""

FUSION = """
flowchart TD
    Q1{"strike ภายใน 30 นาที<br/>ใกล้สนามแค่ไหน?"}
    Q1 -->|"≤ 10 กม."| DANGER["อันตราย<br/>DANGER"]
    Q1 -->|"≤ 13 กม."| SUSPEND["สั่งหยุด<br/>SUSPEND"]
    Q1 -->|"ไม่มีในวง 13 กม."| Q2{"strike ≤ 16 กม. หรือ<br/>เรดาร์ ≥ 40 dBZ ใน 16 กม.?"}
    Q2 -->|ใช่| WATCH["เฝ้าระวัง<br/>WATCH"]
    Q2 -->|ไม่| Q3{"ข้อมูลฟ้าผ่า<br/>พร้อมใช้?"}
    Q3 -->|ไม่พร้อม| NODATA["ไม่มีข้อมูล<br/>NO_DATA"]
    Q3 -->|พร้อม| Q4{"มีแต่เครือข่ายฟรี<br/>ที่จับได้ไม่ครบ?"}
    Q4 -->|"ใช่ และเรดาร์ไม่พร้อม"| NODATA
    Q4 -->|"ไม่ใช่ หรือเรดาร์ยืนยันแล้ว"| CLEAR["ปลอดภัย<br/>ALL_CLEAR"]
    style DANGER fill:#fadbd8,stroke:#e74c3c
    style SUSPEND fill:#fae5d3,stroke:#e8873a
    style WATCH fill:#fcf3cf,stroke:#d4ac0d
    style NODATA fill:#ebdef0,stroke:#a78bda
    style CLEAR fill:#d5f5e3,stroke:#2ecc71
"""

SCREENS = {
    "screen_replay_danger.png": (
        "/?mode=replay&file=capture_20260916T054352Z_storm.jsonl&t=2026-09-16T06:52:00Z&stadium=11&zoom=10",
        (1600, 1000),
    ),
    "screen_live_radar.png": ("/?stadium=8&zoom=7", (1600, 1000)),
}


def edge_screenshot(url: str, target: Path, size: tuple[int, int], budget_ms: int) -> None:
    # โปรไฟล์ใหม่ทุกครั้ง: กัน Edge ใช้ไฟล์ CSS/JS เก่าที่แคชไว้จากรอบก่อน
    profile = tempfile.mkdtemp(prefix="geoai-edge-")
    if target.exists():
        target.unlink()
    subprocess.run(
        [
            str(EDGE),
            "--headless=new",
            "--disable-gpu",
            "--hide-scrollbars",
            f"--user-data-dir={profile}",
            f"--window-size={size[0]},{size[1]}",
            f"--virtual-time-budget={budget_ms}",
            f"--screenshot={target}",
            url,
        ],
        check=False,
        capture_output=True,
        timeout=120,
    )
    # msedge.exe เป็นตัวเปิดที่จบก่อน process จริงเขียนภาพเสร็จ - รอจนไฟล์มีและขนาดนิ่ง
    deadline = time.monotonic() + budget_ms / 1000 + 60
    last_size = -1
    while time.monotonic() < deadline:
        if target.exists():
            size_now = target.stat().st_size
            if size_now > 0 and size_now == last_size:
                return
            last_size = size_now
        time.sleep(1)
    raise RuntimeError(f"Edge ไม่ได้สร้างภาพ {target.name}")


def autocrop(path: Path, margin: int = 30) -> None:
    image = Image.open(path).convert("RGB")
    background = Image.new("RGB", image.size, (255, 255, 255))
    box = ImageChops.difference(image, background).getbbox()
    if box:
        left, top, right, bottom = box
        if right >= image.width - 2 or bottom >= image.height - 2:
            raise RuntimeError(f"{path.name} ถูกตัดขอบ - เพิ่มขนาดหน้าต่างใน render_diagram")
        image.crop((
            max(0, left - margin), max(0, top - margin),
            min(image.width, right + margin), min(image.height, bottom + margin),
        )).save(path)


def render_diagram(name: str, code: str, size: tuple[int, int]) -> None:
    html = Path(tempfile.gettempdir()) / f"geoai_{name}.html"
    html.write_text(MERMAID_PAGE.format(code=json.dumps(code.strip()), width=size[0], height=size[1]), encoding="utf-8")
    target = OUT / f"{name}.png"
    edge_screenshot(html.as_uri(), target, size, budget_ms=15000)
    autocrop(target)
    print(f"  ok  {target.name} {Image.open(target).size}")


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    if not EDGE.exists():
        raise SystemExit(f"ไม่พบ Microsoft Edge ที่ {EDGE} (ตั้ง EDGE_PATH ได้)")

    render_diagram("diagram_architecture", ARCHITECTURE, (3600, 1300))
    render_diagram("diagram_dataflow", DATAFLOW, (3600, 1200))
    render_diagram("diagram_fusion", FUSION, (2000, 3200))

    try:
        urllib.request.urlopen(f"{SERVER}/api/health", timeout=10)
    except OSError:
        raise SystemExit("เปิดเซิร์ฟเวอร์ก่อน: uvicorn app.main:app  แล้วรันสคริปต์นี้ใหม่") from None

    for name, (path, size) in SCREENS.items():
        target = OUT / name
        edge_screenshot(f"{SERVER}{path}", target, size, budget_ms=20000)
        print(f"  ok  {name} {Image.open(target).size}")


if __name__ == "__main__":
    main()
