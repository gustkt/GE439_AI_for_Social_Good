/* GeoAI-for-Disaster — frontend หน้าเดียว (vanilla JS + Leaflet)
 *
 * ข้อสำคัญ: ไฟล์นี้คุยกับ backend ของเราเท่านั้น ไม่เคยเรียก Xweather ตรง ๆ
 * credential จึงไม่มีทางหลุดมาอยู่ในเบราว์เซอร์ และโควตา API ก็ไม่บานปลาย
 * ตามจำนวนผู้ชม (ดู app/ingest.py และ docs/system_design.md)
 *
 * มี 2 โหมด:
 *   สด     - ข้อมูลที่ระบบรับเข้ามาตอนนี้ รีเฟรชทุก 30 วินาที
 *   Replay - พายุจริงที่บันทึกไว้ ดู ณ "เวลาเสมือน" ที่เลื่อน/เล่น/เร่งได้
 *            backend คำนวณสถานะ ณ เวลานั้นด้วยสูตรเดียวกับโหมดสด (app/replay_view.py)
 */

'use strict';

const REFRESH_MS = 30000;
const STRIKE_WINDOW_MINUTES = 30;
const THAI_TZ = 'Asia/Bangkok';
// strike ที่เกิดภายในกี่นาทีถือว่า "เพิ่งเกิด" -> ไอคอนกะพริบ
const FRESH_STRIKE_MINUTES = 5;
// ฟ้าผ่าทั่วประเทศแสดงย้อนหลังนานกว่า เพราะเครือข่ายฟรีจับได้เป็นระยะ ไม่ถี่
const REGION_STRIKE_MINUTES = 60;
// รูปสายฟ้าแบบ SVG (หน้าตาเหมือนกันทุกเครื่อง ต่างจาก emoji ที่แต่ละระบบวาดไม่เหมือนกัน)
const BOLT_PATH = 'M13 2 4 14h7l-1 8 9-12h-7z';

const STATUS_STYLE = {
  DANGER:    { color: '#e74c3c', label: 'อันตราย',   action: 'ทุกคนต้องอยู่ในที่กำบังแล้ว' },
  SUSPEND:   { color: '#e8873a', label: 'สั่งหยุด',   action: 'ระงับการแข่งขัน เริ่มอพยพ' },
  WATCH:     { color: '#f4d03f', label: 'เฝ้าระวัง',  action: 'แจ้ง safety officer เตรียมพร้อม' },
  ALL_CLEAR: { color: '#2ecc71', label: 'ปลอดภัย',   action: 'กลับมาใช้สนามได้' },
  // ไม่ใช่ "ปลอดภัย": แหล่งข้อมูลฟ้าผ่าไม่พร้อม (quota หมด / หลุด) และไม่มีหลักฐานอื่น
  NO_DATA:   { color: '#a78bda', label: 'ไม่มีข้อมูลฟ้าผ่า', action: 'ยังยืนยันไม่ได้ว่าปลอดภัย — ตรวจสอบแหล่งข้อมูล' },
  // ไม่ใช่ระดับความเสี่ยง: แหล่งข้อมูลตอนนี้ไม่ได้ติดตามสนามนี้ จึงไม่รู้ว่าปลอดภัยหรือไม่
  UNMONITORED: { color: '#6b7a89', label: 'ไม่ได้เฝ้าระวัง', action: 'แหล่งข้อมูลตอนนี้ไม่ได้ติดตามสนามนี้' },
};

const state = {
  mode: 'live',
  meta: null,
  risks: [],
  byId: new Map(),
  selectedId: null,
  markers: new Map(),
  ringLayer: L.layerGroup(),
  strikeLayer: L.layerGroup(),
  regionStrikeLayer: L.layerGroup(),
  radarCellLayer: L.layerGroup(),
  radarTileLayer: null,
  radarTemplate: null,
  showRadar: true,
  showRings: true,
  showStrikes: true,
  replay: {
    list: [],
    info: null,
    start: 0,
    end: 0,
    t: 0,
    playing: false,
    speed: 30,
    timer: null,
    busy: false,
    sliderTimer: null,
  },
};

// ศูนย์กลางประเทศไทยคร่าว ๆ ให้เห็นทั้งเชียงรายและสงขลาในจอเดียว
const map = L.map('map', { zoomControl: true }).setView([13.4, 100.9], 6);

L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 18,
  attribution: '&copy; OpenStreetMap contributors',
}).addTo(map);

state.ringLayer.addTo(map);
state.regionStrikeLayer.addTo(map);
state.strikeLayer.addTo(map);
state.radarCellLayer.addTo(map);

/* ------------------------------------------------------------------ utils */

const styleFor = (status) => STATUS_STYLE[status] || STATUS_STYLE.ALL_CLEAR;
const $ = (id) => document.getElementById(id);

async function getJSON(url) {
  const response = await fetch(url, { cache: 'no-store' });
  if (!response.ok) throw new Error(`${url} → HTTP ${response.status}`);
  return response.json();
}

function formatKm(value) {
  return value === null || value === undefined ? '—' : `${value.toFixed(1)} กม.`;
}

function formatClock(seconds) {
  if (seconds === null || seconds === undefined) return '—';
  const total = Math.max(0, Math.round(seconds));
  const mm = String(Math.floor(total / 60)).padStart(2, '0');
  const ss = String(total % 60).padStart(2, '0');
  return `${mm}:${ss}`;
}

function formatEta(risk) {
  if (risk.nowcast_eta_minutes === null || risk.nowcast_eta_minutes === undefined) {
    // null มีความหมาย: nowcast ไม่ยอมเดาเมื่อสัญญาณไม่ชัด — แสดงเหตุผลแทน
    return risk.nowcast_reason ? `— (${risk.nowcast_reason})` : '—';
  }
  if (risk.nowcast_eta_minutes <= 0) return 'ถึงแล้ว';
  return `~${risk.nowcast_eta_minutes} นาที`;
}

// แสดงเวลาไทยเสมอ ไม่ว่าเครื่องที่เปิดหน้าเว็บจะตั้ง timezone อะไร
function formatLocalTime(iso) {
  if (!iso) return '—';
  const date = new Date(iso);
  return Number.isNaN(date.getTime()) ? '—' : date.toLocaleTimeString('th-TH', { timeZone: THAI_TZ });
}

function formatThaiDateTime(ms) {
  return new Date(ms).toLocaleString('th-TH', {
    timeZone: THAI_TZ,
    day: 'numeric',
    month: 'short',
    year: 'numeric',
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  });
}

/* ------------------------------------------------------ URL ตามโหมดปัจจุบัน */

const isReplay = () => state.mode === 'replay' && state.replay.info !== null;
const replayBase = () => `api/replays/${encodeURIComponent(state.replay.info.file)}`;
const virtualIso = () => encodeURIComponent(new Date(state.replay.t).toISOString());
const currentNowMs = () => (isReplay() ? state.replay.t : Date.now());

const urls = {
  risk: () => (isReplay() ? `${replayBase()}/risk?t=${virtualIso()}` : 'api/risk'),
  strikes: (id) =>
    isReplay()
      ? `${replayBase()}/strikes?stadium_id=${id}&minutes=${STRIKE_WINDOW_MINUTES}&t=${virtualIso()}`
      : `api/strikes/recent?stadium_id=${id}&minutes=${STRIKE_WINDOW_MINUTES}`,
  radar: () => (isReplay() ? `${replayBase()}/radar?t=${virtualIso()}` : 'api/radar'),
};

/* ------------------------------------------------------------- แถบข้างต่าง ๆ */

function renderLegend(meta) {
  const radiusByStatus = Object.fromEntries(
    (meta.rings || []).map((ring) => [ring.status, ring.radius_km])
  );
  $('legend').innerHTML = ['DANGER', 'SUSPEND', 'WATCH', 'ALL_CLEAR']
    .map((status) => {
      const style = styleFor(status);
      const radius = radiusByStatus[status];
      const scope = radius ? `≤ ${radius} กม.` : `${meta.all_clear_minutes} นาที`;
      return `<li>
        <span class="swatch" style="background:${style.color}"></span>
        <span>${style.label}</span>
        <span class="radius">${scope}</span>
      </li>`;
    })
    .join('');
}

function renderSourceBadge() {
  // ระบบใช้ข้อมูลจริงเท่านั้น: LIVE = รับสด, REPLAY = ข้อมูลจริงที่บันทึกไว้แล้วเล่นซ้ำ
  const badge = $('source-badge');
  if (isReplay()) {
    badge.className = 'badge badge-replay';
    badge.textContent = `REPLAY ข้อมูลจริง · ${formatThaiDateTime(state.replay.t)}`;
  } else if (state.meta) {
    badge.className = `badge ${state.meta.is_replay ? 'badge-replay' : 'badge-live'}`;
    badge.textContent = state.meta.source_label;
  }
  badge.hidden = false;
}

function renderSummary(summary) {
  const cells = ['DANGER', 'SUSPEND', 'WATCH', 'ALL_CLEAR']
    .map((status) => {
      const style = styleFor(status);
      return `<div class="summary-cell" style="border-top-color:${style.color}">
        <span class="value">${summary[status] ?? 0}</span>
        <span class="label">${style.label}</span>
      </div>`;
    })
    .join('');
  const noData = summary.NO_DATA
    ? `<div class="footnote" style="grid-column:1 / -1;margin:4px 0 0;color:#a78bda">
         ${summary.NO_DATA} สนามไม่มีข้อมูลฟ้าผ่า — ไม่ได้แปลว่าปลอดภัย
       </div>`
    : '';
  const unmonitored = summary.UNMONITORED
    ? `<div class="footnote" style="grid-column:1 / -1;margin:4px 0 0">
         อีก ${summary.UNMONITORED} สนามไม่ได้เฝ้าระวัง (สีเทา) — ไม่ได้แปลว่าปลอดภัย
       </div>`
    : '';
  $('summary-grid').innerHTML = cells + noData + unmonitored;
}

function boltSvg(size) {
  return `<svg viewBox="0 0 24 24" width="${size}" height="${size}" aria-hidden="true"><path d="${BOLT_PATH}"/></svg>`;
}

function renderStadiumList() {
  const list = $('stadium-list');
  list.innerHTML = state.risks
    .map((risk) => {
      const style = styleFor(risk.status);
      const selected = risk.stadium_id === state.selectedId ? ' selected' : '';
      const flag = risk.location_unverified
        ? ' <span class="flag-verify" title="พิกัดยังรอการตรวจสอบ">⚑</span>'
        : '';
      return `<li class="${selected.trim()}" data-id="${risk.stadium_id}"
                  style="border-left-color:${selected ? style.color : 'transparent'}">
        <span class="dot" style="background:${style.color}"></span>
        <span class="names">
          <span class="club">${risk.club}${flag}</span>
          <span class="venue">${risk.name}</span>
        </span>
        ${risk.strike_count
          ? `<span class="bolt-count" title="ฟ้าผ่าใน 30 นาทีล่าสุด">${boltSvg(11)}${risk.strike_count}</span>`
          : ''}
        <span class="dist">${risk.closest_km === null ? '—' : risk.closest_km.toFixed(1)}</span>
      </li>`;
    })
    .join('');

  list.querySelectorAll('li').forEach((item) => {
    item.addEventListener('click', () => selectStadium(Number(item.dataset.id), true));
  });

  $('stadium-count').textContent = `(${state.risks.length})`;
}

function setStatusBar(message, isError) {
  const bar = $('last-update');
  bar.className = isError ? 'error' : '';
  bar.textContent = message;
}

/* ----------------------------------------------------------------- แผนที่ */

function popupHtml(risk) {
  const style = styleFor(risk.status);
  if (risk.status === 'NO_DATA') {
    const why = (risk.status_reasons || []).join('<br>');
    return `<div class="popup">
      <h3>${risk.club}</h3>
      <p class="venue">${risk.name}</p>
      <span class="status-line" style="background:${style.color}22;color:${style.color}">${style.label}</span>
      <p class="warn">${style.action}</p>
      <p class="warn" style="color:#8ba0b4">${why}</p>
    </div>`;
  }
  if (risk.monitored === false) {
    return `<div class="popup">
      <h3>${risk.club}</h3>
      <p class="venue">${risk.name}</p>
      <span class="status-line" style="background:${style.color}22;color:${style.color}">${style.label}</span>
      <p class="warn">${style.action} — สถานะนี้ <b>ไม่ได้</b> แปลว่าปลอดภัย</p>
    </div>`;
  }
  const warn = risk.location_unverified
    ? `<p class="warn">⚑ พิกัดสนามนี้มาจากการ geocode ยังรอการตรวจสอบ
         ตัวเลขระยะทางจึงยังไม่ควรใช้อ้างอิงจริง</p>`
    : '';
  return `<div class="popup">
    <h3>${risk.club}</h3>
    <p class="venue">${risk.name}</p>
    <span class="status-line" style="background:${style.color}22;color:${style.color}">
      ${style.label} — ${style.action}
    </span>
    <dl>
      <dt>strike ที่ใกล้สุด</dt><dd>${formatKm(risk.closest_km)}</dd>
      <dt>strike ใน 30 นาที</dt><dd>${risk.strike_count}</dd>
      <dt>คาดว่าจะถึงสนาม</dt><dd>${formatEta(risk)}</dd>
      <dt>นับถอยหลัง all-clear</dt><dd>${formatClock(risk.all_clear_seconds_remaining)}</dd>
      <dt>ความเร็วพายุ</dt><dd>${
        risk.nowcast_speed_kmph ? `${risk.nowcast_speed_kmph} กม./ชม.` : '—'
      }</dd>
      <dt>เรดาร์: ฝนแรงใกล้สุด</dt><dd>${
        risk.radar_available ? formatKm(risk.radar_nearest_hotspot_km) : 'ไม่มีข้อมูล'
      }</dd>
      <dt>เรดาร์: dBZ สูงสุดในวง 16 กม.</dt><dd>${risk.radar_max_dbz_watch ?? '—'}</dd>
      <dt>ฝนแรงคาดว่าถึงวง 16 กม.</dt><dd>${
        formatEta({ nowcast_eta_minutes: risk.radar_eta_minutes, nowcast_reason: risk.radar_eta_reason })
      }</dd>
    </dl>
    ${(risk.status_reasons || []).length
      ? `<p class="warn" style="color:#8ba0b4">${risk.status_reasons.join('<br>')}</p>`
      : ''}
    ${warn}
  </div>`;
}

function upsertMarker(risk) {
  const style = styleFor(risk.status);
  const existing = state.markers.get(risk.stadium_id);

  if (existing) {
    existing.setStyle({ fillColor: style.color, color: style.color });
    existing.setPopupContent(popupHtml(risk));
    return;
  }

  const marker = L.circleMarker([risk.lat, risk.lon], {
    radius: 8,
    weight: 2,
    color: style.color,
    fillColor: style.color,
    fillOpacity: 0.75,
  })
    .bindPopup(popupHtml(risk))
    .addTo(map);

  marker.on('click', () => selectStadium(risk.stadium_id, false));
  state.markers.set(risk.stadium_id, marker);
}

function drawRings(risk) {
  state.ringLayer.clearLayers();
  if (!state.showRings || !risk || !state.meta) return;

  // วาดวงนอกก่อน เพื่อให้วงในทับอยู่ด้านบนและอ่านง่าย
  [...state.meta.rings]
    .sort((a, b) => b.radius_km - a.radius_km)
    .forEach((ring) => {
      L.circle([risk.lat, risk.lon], {
        radius: ring.radius_km * 1000,
        color: styleFor(ring.status).color,
        weight: 1.5,
        opacity: 0.85,
        fillOpacity: 0.05,
        dashArray: '5,6',
      })
        .bindTooltip(`${styleFor(ring.status).label} — ${ring.radius_km} กม.`)
        .addTo(state.ringLayer);
    });
}

function boltIcon(freshness, fresh) {
  // ใหม่ = ใหญ่และทึบ, เก่า = เล็กและจาง -> มองแล้วเห็นทิศที่พายุกำลังเคลื่อนไป
  const size = Math.round(14 + 12 * freshness);
  return L.divIcon({
    className: fresh ? 'bolt-icon bolt-fresh' : 'bolt-icon',
    html: `<svg viewBox="0 0 24 24" width="${size}" height="${size}" style="opacity:${freshness.toFixed(2)}">` +
      `<path d="${BOLT_PATH}"/></svg>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

async function drawStrikes(stadiumId) {
  if (!state.showStrikes || stadiumId === null) {
    state.strikeLayer.clearLayers();
    return;
  }

  const payload = await getJSON(urls.strikes(stadiumId));
  state.strikeLayer.clearLayers();

  const now = currentNowMs();
  payload.strikes.forEach((strike) => {
    // strike เก่าจางลงตามอายุ ทำให้ "เห็นทิศทาง" ที่พายุกำลังเคลื่อนไป
    const ageMinutes = (now - new Date(strike.ts_iso).getTime()) / 60000;
    const freshness = Math.max(0.12, 1 - ageMinutes / STRIKE_WINDOW_MINUTES);
    L.marker([strike.lat, strike.lon], {
      icon: boltIcon(freshness, ageMinutes <= FRESH_STRIKE_MINUTES),
      keyboard: false,
    })
      .bindTooltip(
        `${formatKm(strike.distance_km)} · ${formatLocalTime(strike.ts_iso)}<br>` +
          `แหล่ง: ${strike.source}`
      )
      .addTo(state.strikeLayer);
  });
}

async function drawRegionStrikes() {
  // ฟ้าผ่าจริงทั่วประเทศในชั่วโมงล่าสุด (โหมดสดเท่านั้น) - เห็นว่าข้อมูลสดไหลเข้าจริงแม้ไม่มีพายุใกล้สนาม
  state.regionStrikeLayer.clearLayers();
  if (isReplay() || !state.showStrikes) return;
  const payload = await getJSON(`api/strikes/region?minutes=${REGION_STRIKE_MINUTES}`);
  const now = Date.now();
  payload.strikes.forEach((strike) => {
    const ageMinutes = (now - new Date(strike.ts_iso).getTime()) / 60000;
    const freshness = Math.max(0.25, 1 - ageMinutes / REGION_STRIKE_MINUTES);
    L.marker([strike.lat, strike.lon], {
      icon: boltIcon(freshness * 0.6, ageMinutes <= FRESH_STRIKE_MINUTES),
      keyboard: false,
    })
      .bindTooltip(`ฟ้าผ่าจริง ${formatLocalTime(strike.ts_iso)} · แหล่ง: ${strike.source}`)
      .addTo(state.regionStrikeLayer);
  });
}

async function drawRadar() {
  const radar = await getJSON(urls.radar());
  state.radarCellLayer.clearLayers();

  // ภาพเรดาร์สดของ RainViewer มีเฉพาะโหมดสด (ภาพย้อนหลังเกิน 2 ชม. หมดอายุไปแล้ว)
  // ต้องเอาออกตอน replay ไม่งั้นจะเห็นฝนของวันนี้ซ้อนบนพายุของวันอื่น
  const wantTiles = state.showRadar && radar.tile_url_template;
  if (!wantTiles) {
    if (state.radarTileLayer) map.removeLayer(state.radarTileLayer);
    state.radarTileLayer = null;
    state.radarTemplate = null;
  } else if (radar.tile_url_template !== state.radarTemplate) {
    if (state.radarTileLayer) map.removeLayer(state.radarTileLayer);
    state.radarTileLayer = L.tileLayer(radar.tile_url_template, {
      opacity: 0.55,
      maxNativeZoom: radar.tile_max_zoom,
      maxZoom: 18,
      zIndex: 5,
      attribution: 'Radar &copy; <a href="https://www.rainviewer.com">RainViewer</a>',
    }).addTo(map);
    state.radarTemplate = radar.tile_url_template;
  }

  if (!state.showRadar) return;

  // cell ฝนแรงที่ระบบตรวจพบ (ผลวิเคราะห์ของเรา ไม่ใช่ภาพดิบ)
  (radar.cells || []).forEach((cell) => {
    const color = cell.max_dbz >= 50 ? '#c10000' : cell.max_dbz >= 45 ? '#ff4400' : '#ffaa00';
    L.circleMarker([cell.lat, cell.lon], {
      radius: Math.min(14, 4 + Math.sqrt(cell.pixels)),
      color,
      weight: 2,
      fillColor: color,
      fillOpacity: 0.2,
    })
      .bindTooltip(`cell ฝนแรง ${cell.max_dbz} dBZ · ${cell.area_km2} ตร.กม.`)
      .addTo(state.radarCellLayer);
  });
}

/* ---------------------------------------------------------- วาดผลตามโหมด */

async function renderRisk() {
  const payload = await getJSON(urls.risk());
  state.risks = payload.results;
  state.byId = new Map(payload.results.map((risk) => [risk.stadium_id, risk]));

  renderSummary(payload.summary);
  state.risks.forEach(upsertMarker);
  renderStadiumList();

  if (state.selectedId !== null) {
    drawRings(state.byId.get(state.selectedId));
    await drawStrikes(state.selectedId);
  }

  try {
    await drawRegionStrikes();
  } catch (regionError) {
    console.error('โหลดฟ้าผ่าทั่วประเทศไม่สำเร็จ', regionError);
  }

  try {
    await drawRadar();
  } catch (radarError) {
    console.error('โหลดเรดาร์ไม่สำเร็จ', radarError);
  }
  return payload;
}

async function selectStadium(stadiumId, panTo) {
  state.selectedId = stadiumId;
  const risk = state.byId.get(stadiumId);
  renderStadiumList();
  drawRings(risk);

  if (panTo && risk) {
    map.setView([risk.lat, risk.lon], Math.max(map.getZoom(), 10));
    state.markers.get(stadiumId)?.openPopup();
  }

  try {
    await drawStrikes(stadiumId);
  } catch (error) {
    console.error('โหลด strike ไม่สำเร็จ', error);
  }
}

/* -------------------------------------------------------------- โหมดสด */

async function refresh() {
  if (state.mode !== 'live') return;
  try {
    await renderRisk();

    const meta = await getJSON('api/meta');
    state.meta = meta;
    renderSourceBadge();
    const stamp = new Date().toLocaleTimeString('th-TH', { timeZone: THAI_TZ });
    const ingest = meta.ingest || {};
    const radarText = meta.radar_available
      ? `เรดาร์: ภาพ ${formatLocalTime(meta.radar_frame_ts)}`
      : `เรดาร์ไม่พร้อม — ${meta.radar_unavailable_reason}`;
    const lightningError = meta.lightning_available === false
      ? `ไม่มีข้อมูลฟ้าผ่า — ${meta.lightning_unavailable_reason}`
      : meta.source_error;
    setStatusBar(
      lightningError
        ? `${lightningError} · ${radarText}`
        : `อัปเดตเมื่อ ${stamp} · strike ใกล้สนาม ${ingest.stored_total ?? 0}` +
          (meta.lightning_sparse ? ' (เครือข่ายฟรี จับได้ไม่ครบ ใช้เรดาร์ยืนยันก่อนขึ้นปลอดภัย)' : '') +
          ` · ${radarText}`,
      Boolean(lightningError)
    );
  } catch (error) {
    console.error(error);
    setStatusBar(`เชื่อมต่อ backend ไม่ได้: ${error.message}`, true);
  }
}

/* ------------------------------------------------------------ โหมด replay */

function setModeButtons() {
  $('mode-live').classList.toggle('active', state.mode === 'live');
  $('mode-replay').classList.toggle('active', state.mode === 'replay');
  $('replay-controls').hidden = state.mode !== 'replay';
}

function syncReplayControls() {
  const r = state.replay;
  $('replay-clock').textContent = r.info ? formatThaiDateTime(r.t) : '—';
  $('replay-slider').value = String(Math.round((r.t - r.start) / 1000));
  renderSourceBadge();
}

async function renderReplayFrame() {
  const r = state.replay;
  if (!isReplay() || r.busy) return;
  r.busy = true;
  try {
    const payload = await renderRisk();
    syncReplayControls();
    const lightning = payload.lightning_available
      ? 'ฟ้าผ่า: มีข้อมูล'
      : `ฟ้าผ่า: ${payload.lightning_unavailable_reason}`;
    const radar = payload.radar_available ? 'เรดาร์: มีข้อมูล' : `เรดาร์: ${payload.radar_unavailable_reason}`;
    setStatusBar(`REPLAY ${formatThaiDateTime(r.t)} · ${lightning} · ${radar}`, false);
  } catch (error) {
    console.error(error);
    setStatusBar(`โหลด replay ไม่สำเร็จ: ${error.message}`, true);
  } finally {
    r.busy = false;
  }
}

function pauseReplay() {
  const r = state.replay;
  clearInterval(r.timer);
  r.timer = null;
  r.playing = false;
  $('replay-play').textContent = '▶ เล่น';
}

function playReplay() {
  const r = state.replay;
  if (!r.info) return;
  if (r.t >= r.end) r.t = r.start;
  r.playing = true;
  $('replay-play').textContent = '⏸ หยุด';
  r.timer = setInterval(async () => {
    if (r.busy) return; // รอบก่อนยังโหลดไม่เสร็จ ไม่เลื่อนเวลาข้ามไป
    r.t = Math.min(r.end, r.t + r.speed * 1000);
    if (r.t >= r.end) pauseReplay();
    await renderReplayFrame();
  }, 1000);
}

function describeReplay(info) {
  const peak = info.peak
    ? `ใกล้สนามที่สุด ${info.peak.closest_km.toFixed(1)} กม. (${info.peak.name}) เวลา ${formatLocalTime(info.peak.closest_at)}`
    : 'ไม่มี strike ใกล้สนาม';
  const radar = info.radar_frames
    ? `ภาพเรดาร์ ${info.radar_frames} ภาพ`
    : 'ไฟล์นี้ไม่มีข้อมูลเรดาร์ (บันทึกก่อนเพิ่มโมดูลเรดาร์)';
  return `ข้อมูลจริงจาก ${info.sources.join(', ') || '-'} · strike ${info.strikes} จุด · ${peak} · ${radar}. ` +
    'สถานะคำนวณ ณ เวลาเสมือน ความเร็วพายุและนาฬิกา all-clear จึงตรงความจริงแม้เร่งความเร็ว';
}

async function selectReplay(file) {
  const r = state.replay;
  pauseReplay();
  r.info = r.list.find((item) => item.file === file) || null;
  if (!r.info) return;

  r.start = Date.parse(r.info.view_start);
  r.end = Date.parse(r.info.view_end);
  r.t = r.start;
  const slider = $('replay-slider');
  slider.min = '0';
  slider.max = String(Math.round((r.end - r.start) / 1000));
  slider.step = '30';
  $('replay-note').textContent = describeReplay(r.info);
  $('replay-peak').disabled = !r.info.peak;

  await renderReplayFrame();
  // ซูมไปสนามที่พายุเข้าใกล้ที่สุด เพื่อให้เห็นเหตุการณ์ทันที
  const focus = r.info.peak ? r.info.peak.stadium_id : state.risks[0]?.stadium_id;
  if (focus !== undefined && focus !== null) await selectStadium(focus, true);
}

async function enterReplay() {
  state.mode = 'replay';
  setModeButtons();
  const r = state.replay;
  if (r.list.length === 0) {
    const data = await getJSON('api/replays');
    r.list = data.replays;
    $('replay-file').innerHTML = r.list
      .map((item) => `<option value="${item.file}">${item.label} · ${item.strikes} strike</option>`)
      .join('');
  }
  if (r.list.length === 0) {
    $('replay-note').textContent = 'ยังไม่มีไฟล์ข้อมูลจริงที่บันทึกไว้ — เปิด CAPTURE=true ช่วงที่มีพายุ';
    return;
  }
  await selectReplay($('replay-file').value || r.list[0].file);
}

async function exitReplay() {
  pauseReplay();
  state.mode = 'live';
  setModeButtons();
  map.closePopup();
  await refresh();
}

function wireReplay() {
  $('mode-live').addEventListener('click', () => {
    if (state.mode !== 'live') exitReplay().catch(console.error);
  });
  $('mode-replay').addEventListener('click', () => {
    if (state.mode !== 'replay') {
      enterReplay().catch((error) => setStatusBar(`เปิด replay ไม่สำเร็จ: ${error.message}`, true));
    }
  });
  $('replay-file').addEventListener('change', (event) => {
    selectReplay(event.target.value).catch(console.error);
  });
  $('replay-play').addEventListener('click', () => {
    if (state.replay.playing) pauseReplay();
    else playReplay();
  });
  $('replay-speed').addEventListener('change', (event) => {
    state.replay.speed = Number(event.target.value);
  });
  $('replay-slider').addEventListener('input', (event) => {
    const r = state.replay;
    r.t = r.start + Number(event.target.value) * 1000;
    $('replay-clock').textContent = formatThaiDateTime(r.t);
    // ลากแถบเวลาแล้วรอให้หยุดลากสักครู่ค่อยโหลด ไม่ยิง API ทุก pixel ที่ลาก
    clearTimeout(r.sliderTimer);
    r.sliderTimer = setTimeout(() => renderReplayFrame(), 200);
  });
  $('replay-peak').addEventListener('click', () => {
    const r = state.replay;
    if (!r.info?.peak) return;
    r.t = Math.min(r.end, Math.max(r.start, Date.parse(r.info.peak.closest_at) + 60000));
    renderReplayFrame();
  });
}

/* ------------------------------------------------------------- deep link */

// เปิดหน้าเว็บตรงไปยังสถานะที่ต้องการได้ด้วย URL เช่น
//   ?mode=replay&file=capture_20260916T054352Z_storm.jsonl&t=2026-09-16T06:52:00Z&stadium=11
// ใช้ทำภาพประกอบรายงาน และให้ผู้ตรวจเปิดเหตุการณ์สำคัญได้ในคลิกเดียว
async function applyDeepLink() {
  const params = new URLSearchParams(window.location.search);
  if (params.get('mode') === 'replay') {
    await enterReplay();
    const r = state.replay;
    const file = params.get('file');
    if (file && file !== r.info?.file && r.list.some((item) => item.file === file)) {
      $('replay-file').value = file;
      await selectReplay(file);
    }
    const t = Date.parse(params.get('t') || '');
    if (!Number.isNaN(t) && r.info) {
      r.t = Math.min(r.end, Math.max(r.start, t));
      await renderReplayFrame();
    }
  }
  const stadium = Number(params.get('stadium'));
  const zoom = Number(params.get('zoom'));
  if (stadium && state.byId.has(stadium)) {
    // ย้ายแผนที่ครั้งเดียวแบบไม่มี animation: ถ้าสั่ง pan แล้วตามด้วย setZoom
    // animation ของ pan จะถูกยกเลิก แผนที่ซูมค้างอยู่ที่จุดเดิม (เช่น กรุงเทพฯ)
    await selectStadium(stadium, false);
    const risk = state.byId.get(stadium);
    map.setView([risk.lat, risk.lon], zoom || 10, { animate: false });
    state.markers.get(stadium)?.openPopup();
  } else if (zoom) {
    map.setZoom(zoom);
  }
}

/* ------------------------------------------------------------------- init */

function wireToggles() {
  $('toggle-rings').addEventListener('change', (event) => {
    state.showRings = event.target.checked;
    drawRings(state.byId.get(state.selectedId));
  });

  $('toggle-strikes').addEventListener('change', (event) => {
    state.showStrikes = event.target.checked;
    drawStrikes(state.selectedId).catch(console.error);
    drawRegionStrikes().catch(console.error);
  });

  $('toggle-radar').addEventListener('change', (event) => {
    state.showRadar = event.target.checked;
    drawRadar().catch(console.error);
  });
}

async function init() {
  wireToggles();
  wireReplay();

  state.meta = await getJSON('api/meta');
  renderLegend(state.meta);
  renderSourceBadge();

  await refresh();

  // เลือกสนามที่เสี่ยงที่สุดให้อัตโนมัติ — เปิดหน้ามาก็เห็นเรื่องที่สำคัญที่สุดเลย
  if (state.risks.length > 0) {
    const alerting = ['DANGER', 'SUSPEND', 'WATCH'].includes(state.risks[0].status);
    await selectStadium(state.risks[0].stadium_id, alerting);
  }

  await applyDeepLink();
  setInterval(refresh, REFRESH_MS);
}

init().catch((error) => {
  console.error(error);
  setStatusBar(`เริ่มระบบไม่สำเร็จ: ${error.message}`, true);
});
