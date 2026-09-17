"""สร้างกราฟและตัวเลขสำหรับรายงาน จากข้อมูลจริงที่บันทึกไว้เท่านั้น

รัน:  python -m docs.build.make_figures      (จากโฟลเดอร์โปรเจกต์)

ผลลัพธ์ (docs/figures/):
  storm_timeline.png   ระยะ strike จากสนาม + สถานะของระบบตามเวลา (พายุ 16 ก.ย. 2026)
  storm_map.png        ตำแหน่ง strike รอบสนาม ไล่สีตามเวลา + วงรัศมี + ทิศการเคลื่อนที่
  radar_summary.png    ผลวิเคราะห์ภาพเรดาร์จริงที่บันทึกไว้ (จำนวน cell / dBZ)
  stats.json           ตัวเลขที่ใช้ในเนื้อหารายงาน (คำนวณจากข้อมูล ไม่ได้พิมพ์เอง)

สถานะในกราฟคำนวณด้วยฟังก์ชันเดียวกับที่ระบบใช้จริง (app/replay_view.py -> app/risk.py)
"""

from __future__ import annotations

import json
import math
import os
import re
import subprocess
import sys
from collections import Counter
from datetime import timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.patches import Circle, Patch  # noqa: E402

from app import config, db  # noqa: E402
from app.geo import to_local_xy  # noqa: E402
from app.replay_view import timeline  # noqa: E402
from app.risk import DATA_WINDOW_MINUTES, RADAR_WINDOW_MINUTES, risk_for  # noqa: E402
from app.sources.replay import load_capture  # noqa: E402
from app.timeutil import parse_iso, to_iso, utcnow  # noqa: E402

ROOT = Path(__file__).resolve().parents[2]
OUT = ROOT / "docs" / "figures"
STORM_FILE = "capture_20260916T054352Z_storm.jsonl"
THAI = timezone(timedelta(hours=7))

STATUS_COLOUR = {
    "DANGER": "#e74c3c",
    "SUSPEND": "#e8873a",
    "WATCH": "#f4d03f",
    "ALL_CLEAR": "#2ecc71",
    "NO_DATA": "#a78bda",
}
STATUS_THAI = {
    "DANGER": "อันตราย",
    "SUSPEND": "สั่งหยุด",
    "WATCH": "เฝ้าระวัง",
    "ALL_CLEAR": "ปลอดภัย",
    "NO_DATA": "ไม่มีข้อมูล",
}


def setup_font() -> None:
    fonts = Path(r"C:\Windows\Fonts")
    for name in ("THSarabunNew.ttf", "THSarabunNew Bold.ttf"):
        if (fonts / name).exists():
            font_manager.fontManager.addfont(str(fonts / name))
    plt.rcParams.update({
        "font.family": "TH Sarabun New",
        "font.size": 17,
        "axes.titlesize": 20,
        "axes.labelsize": 18,
        "figure.dpi": 150,
        "savefig.dpi": 200,
        "axes.spines.top": False,
        "axes.spines.right": False,
    })


def thai(moment) -> str:
    return moment.astimezone(THAI).strftime("%H:%M")


# ------------------------------------------------------------------ พายุ 16 ก.ย.


def storm_analysis() -> dict:
    tl = timeline(STORM_FILE)
    stadiums = db.list_stadiums(active_only=True)
    info = tl.describe(stadiums)
    stadium = db.get_stadium(info["peak"]["stadium_id"])

    # สถานะทีละนาที ด้วยฟังก์ชันเดียวกับระบบจริง
    rows = []
    moment = tl.view_start
    while moment <= tl.view_end:
        row = risk_for(
            stadium,
            tl.strikes_near(stadium, moment, DATA_WINDOW_MINUTES),
            True,
            tl.lightning_availability(moment),
            tl.radar_availability(moment),
            tl.radar_history(stadium["id"], moment, RADAR_WINDOW_MINUTES),
            now=moment,
        )
        rows.append((moment, row))
        moment += timedelta(minutes=1)

    transitions = []
    previous = None
    for moment, row in rows:
        if row["status"] != previous:
            transitions.append({
                "time": thai(moment),
                "status": row["status"],
                "status_thai": STATUS_THAI[row["status"]],
                "closest_km": row["closest_km"],
                "strike_count": row["strike_count"],
                "speed_kmph": row["nowcast_speed_kmph"],
                "all_clear_seconds": row["all_clear_seconds_remaining"],
            })
            previous = row["status"]

    minutes_in = Counter(row["status"] for _, row in rows if tl.first_record <= _ <= tl.last_record + timedelta(minutes=30))
    speeds = [row["nowcast_speed_kmph"] for _, row in rows if row["nowcast_speed_kmph"]]
    moving = [row for _, row in rows if row["nowcast_bearing_deg"] is not None]
    busiest = max(moving, key=lambda row: row["strike_count"] or 0) if moving else None

    near = [
        (parse_iso(s["ts_iso"]), s)
        for s in tl.strikes_near(stadium, tl.view_end, (tl.view_end - tl.view_start).total_seconds() / 60)
    ]
    near.sort(key=lambda item: item[0])

    draw_timeline(stadium, rows, near)
    draw_map(stadium, near, rows)

    closest = min(near, key=lambda item: item[1]["distance_km"])
    return {
        "file": STORM_FILE,
        "date_thai": tl.first_record.astimezone(THAI).strftime("%d/%m/%Y"),
        "stadium": stadium["name"],
        "club": stadium["club"],
        "strikes_total": info["strikes"],
        "strikes_near_stadium": len(near),
        "sources": info["sources"],
        "first_strike": thai(tl.first_record),
        "last_strike": thai(tl.last_record),
        "closest_km": round(closest[1]["distance_km"], 2),
        "closest_time": thai(closest[0]),
        "affected_stadiums": info["stadiums"],
        "transitions": transitions,
        "minutes_by_status": dict(minutes_in),
        "nowcast_speed_min": min(speeds) if speeds else None,
        "nowcast_speed_max": max(speeds) if speeds else None,
        "nowcast_speed_median": sorted(speeds)[len(speeds) // 2] if speeds else None,
        "nowcast_bearing_busiest": busiest["nowcast_bearing_deg"] if busiest else None,
        "nowcast_speed_busiest": busiest["nowcast_speed_kmph"] if busiest else None,
    }


def draw_timeline(stadium: dict, rows: list, near: list) -> None:
    fig, (band, ax) = plt.subplots(
        2, 1, figsize=(10, 5.2), sharex=True, gridspec_kw={"height_ratios": [1, 5], "hspace": 0.08}
    )

    # แถบสถานะของระบบ
    for (start, row), (end, _) in zip(rows, rows[1:] + [(rows[-1][0] + timedelta(minutes=1), None)]):
        band.axvspan(start, end, color=STATUS_COLOUR[row["status"]], lw=0)
    band.set_yticks([])
    band.set_ylabel("สถานะ", rotation=0, ha="right", va="center")
    for side in ("left", "bottom"):
        band.spines[side].set_visible(False)
    used = [s for s in STATUS_COLOUR if any(row["status"] == s for _, row in rows)]
    band.legend(
        handles=[Patch(color=STATUS_COLOUR[s], label=STATUS_THAI[s]) for s in used],
        loc="lower center", bbox_to_anchor=(0.5, 1.02), ncol=len(used), frameon=False,
    )

    # ระยะของ strike แต่ละครั้ง
    times = [moment for moment, _ in near]
    distances = [s["distance_km"] for _, s in near]
    ax.scatter(times, distances, s=46, marker="*", color="#d4a017", edgecolor="#5c3a00", lw=0.5,
               zorder=3, label="ฟ้าผ่า (Xweather)")
    for km, key, text in ((16, "WATCH", "16 กม. เฝ้าระวัง"), (13, "SUSPEND", "13 กม. สั่งหยุด (NCAA)"),
                          (10, "DANGER", "10 กม. อันตราย")):
        ax.axhline(km, color=STATUS_COLOUR[key], ls="--", lw=1.4)
        ax.text(rows[-1][0], km + 0.4, text, ha="right", va="bottom", color=STATUS_COLOUR[key], fontsize=15)

    ax.set_ylim(0, config.INGEST_RADIUS_KM + 1)
    ax.set_ylabel("ระยะจากสนาม (กม.)")
    ax.set_xlabel("เวลา (น.) วันที่ 16 ก.ย. 2026")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=THAI))
    ax.xaxis.set_major_locator(mdates.MinuteLocator(byminute=range(0, 60, 15)))
    ax.grid(axis="y", alpha=0.25)
    ax.legend(loc="upper left", frameon=False)
    fig.savefig(OUT / "storm_timeline.png", bbox_inches="tight")
    plt.close(fig)


def draw_map(stadium: dict, near: list, rows: list) -> None:
    fig, ax = plt.subplots(figsize=(7.2, 6.4))
    start = near[0][0]
    xs, ys, age = [], [], []
    for moment, s in near:
        x, y = to_local_xy(s["lat"], s["lon"], stadium["lat"], stadium["lon"])
        xs.append(x)
        ys.append(y)
        age.append((moment - start).total_seconds() / 60)

    for km, key in ((16, "WATCH"), (13, "SUSPEND"), (10, "DANGER")):
        ax.add_patch(Circle((0, 0), km, fill=False, ls="--", lw=1.4, color=STATUS_COLOUR[key]))
        ax.text(0, km + 0.5, f"{km} กม.", ha="center", va="bottom", color=STATUS_COLOUR[key], fontsize=15)

    points = ax.scatter(xs, ys, c=age, cmap="plasma", s=90, marker="*", edgecolor="#333", lw=0.4, zorder=3)
    ax.scatter([0], [0], s=160, marker="s", color="#1f4e79", zorder=4, label=f"สนาม {stadium['name']}")

    # ทิศการเคลื่อนที่ที่ nowcast ประมาณได้ในช่วงที่ข้อมูลมากที่สุด
    moving = [row for _, row in rows if row["nowcast_bearing_deg"] is not None]
    if moving:
        best = max(moving, key=lambda row: row["strike_count"] or 0)
        bearing = math.radians(best["nowcast_bearing_deg"])
        length = 9
        ax.annotate(
            "", xy=(-18 + length * math.sin(bearing), -14 + length * math.cos(bearing)), xytext=(-18, -14),
            arrowprops={"arrowstyle": "-|>", "lw": 2.2, "color": "#1f4e79"},
        )
        ax.text(-18, -15.5, f"ทิศการเคลื่อนที่ (nowcast)\n~{best['nowcast_speed_kmph']:.0f} กม./ชม.",
                ha="center", va="top", fontsize=14, color="#1f4e79")

    limit = config.INGEST_RADIUS_KM
    ax.set_xlim(-limit, limit)
    ax.set_ylim(-limit, limit)
    ax.set_aspect("equal")
    ax.set_xlabel("ระยะไปทางตะวันออก (กม.)  ค่าลบ = ตะวันตก")
    ax.set_ylabel("ระยะไปทางเหนือ (กม.)  ค่าลบ = ใต้")
    ax.grid(alpha=0.2)
    ax.legend(loc="upper left", frameon=False)
    bar = fig.colorbar(points, ax=ax, shrink=0.8)
    bar.set_label(f"นาทีหลังฟ้าผ่าครั้งแรก ({thai(start)} น.)")
    fig.savefig(OUT / "storm_map.png", bbox_inches="tight")
    plt.close(fig)


# ------------------------------------------------------------------------ เรดาร์


def radar_analysis() -> dict:
    frames = {}
    for path in sorted(config.CAPTURE_DIR.glob("*.jsonl")):
        for record in load_capture(path):
            if record.get("kind") == "radar":
                frames[record["frame_ts"]] = record
    if not frames:
        return {"frames": 0}

    ordered = [frames[key] for key in sorted(frames)]
    times = [parse_iso(f["frame_ts"]) for f in ordered]
    cell_counts = [len(f.get("cells") or []) for f in ordered]
    max_dbz = [f.get("max_dbz") or 0 for f in ordered]
    cell_dbz = [c["max_dbz"] for f in ordered for c in f.get("cells") or []]
    nearest = [
        (e["nearest_hotspot_km"], e["stadium_id"], f["frame_ts"])
        for f in ordered for e in f.get("exposures") or [] if e.get("nearest_hotspot_km") is not None
    ]

    fig, (left, right) = plt.subplots(1, 2, figsize=(10, 3.9), gridspec_kw={"width_ratios": [3, 2], "wspace": 0.55})
    left.bar(times, cell_counts, width=timedelta(minutes=7), color="#4a9fd8", label="จำนวน cell ≥ 40 dBZ")
    left.set_ylabel("จำนวน cell")
    twin = left.twinx()
    # ไม่ลากเส้นข้ามช่วงที่เซิร์ฟเวอร์ปิด (ไม่มีภาพ) - ใส่ NaN คั่นเมื่อภาพห่างกันเกิน 30 นาที
    line_t, line_v = [], []
    for i, (moment, value) in enumerate(zip(times, max_dbz)):
        if i and (moment - times[i - 1]) > timedelta(minutes=30):
            line_t.append(moment)
            line_v.append(float("nan"))
        line_t.append(moment)
        line_v.append(value)
    twin.plot(line_t, line_v, color="#c10000", marker="o", ms=3.5, lw=1.4, label="dBZ สูงสุดของภาพ")
    twin.set_ylabel("dBZ สูงสุด")
    twin.spines["right"].set_visible(True)
    left.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=THAI))
    # ภาพครอบหลายชั่วโมง - ติดป้ายทุก 2 ชม. ไม่ให้ตัวเลขเวลาซ้อนกัน
    left.xaxis.set_major_locator(mdates.HourLocator(interval=2, tz=THAI))
    left.set_xlabel(f"เวลา (น.) วันที่ {times[0].astimezone(THAI):%d/%m/%Y}")
    left.set_title("ภาพเรดาร์จริงที่วิเคราะห์ (ทุก 10 นาที)")
    handles = left.get_legend_handles_labels()[0] + twin.get_legend_handles_labels()[0]
    left.legend(handles=handles, loc="upper center", frameon=True, facecolor="white", edgecolor="none", fontsize=13)

    right.hist(cell_dbz, bins=range(40, max(cell_dbz) + 3, 2), color="#e8873a", edgecolor="white")
    right.set_xlabel("dBZ สูงสุดของ cell")
    right.set_ylabel("จำนวน cell")
    right.set_title("การกระจายความแรงของ cell")
    fig.savefig(OUT / "radar_summary.png", bbox_inches="tight")
    plt.close(fig)

    closest = min(nearest) if nearest else None
    closest_stadium = db.get_stadium(closest[1]) if closest else None
    return {
        "frames": len(ordered),
        "first_frame": f"{times[0].astimezone(THAI):%d/%m/%Y %H:%M}",
        "last_frame": f"{times[-1].astimezone(THAI):%d/%m/%Y %H:%M}",
        "cells_total": len(cell_dbz),
        "cells_max_per_frame": max(cell_counts),
        "max_dbz": max(max_dbz),
        "closest_hotspot_km": round(closest[0], 1) if closest else None,
        "closest_hotspot_stadium": closest_stadium["name"] if closest_stadium else None,
        "closest_hotspot_time": f"{parse_iso(closest[2]).astimezone(THAI):%d/%m %H:%M}" if closest else None,
        # คู่ (ภาพ, สนาม) ที่ฝนแรงเข้าวงเฝ้าระวัง = ครั้งที่เรดาร์ทำให้สนามขึ้น WATCH
        "watch_exposures": sum(1 for km, _, _ in nearest if km <= config.RING_WATCH_KM),
        "watch_stadiums": len({sid for km, sid, _ in nearest if km <= config.RING_WATCH_KM}),
    }


# ------------------------------------------------------------------------ tests


def test_summary() -> dict:
    suites = ["test_model", "test_blitzortung", "test_pipeline", "test_availability", "test_radar", "test_replay_view"]
    results = {}
    for suite in suites:
        output = subprocess.run(
            [sys.executable, "-m", f"tests.{suite}"], cwd=ROOT, capture_output=True, text=True,
            encoding="utf-8", env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        ).stdout
        match = re.search(r"ผ่านทั้งหมด (\d+) ข้อ", output)
        results[suite] = int(match.group(1)) if match else 0
    return {"suites": results, "total": sum(results.values())}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    setup_font()
    stats = {
        "generated_at": to_iso(utcnow()),
        "storm": storm_analysis(),
        "radar": radar_analysis(),
        "tests": test_summary(),
        "stadiums_active": len(db.list_stadiums(active_only=True)),
    }
    (OUT / "stats.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
