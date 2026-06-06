'use strict';

let sessionId   = null;
let stops       = [];
let currentIdx  = 0;
let totalTime   = '';
let lastMetaPc  = null;
let metaOpen    = false;

// GPS / compass state
let deviceLat    = null;
let deviceLon    = null;
let deviceHeading = null;
const THROAT_SWITCH_M = 20;   // switch arrow to address when within this distance of throat

// ── GPS + compass ─────────────────────────────────────────────────────────────

if (navigator.geolocation) {
  navigator.geolocation.watchPosition(
    pos => {
      deviceLat = pos.coords.latitude;
      deviceLon = pos.coords.longitude;
      if (pos.coords.heading != null) deviceHeading = pos.coords.heading;
      refreshArrow();
    },
    () => {},
    { enableHighAccuracy: true, maximumAge: 2000 },
  );
}

window.addEventListener('deviceorientationabsolute', e => {
  if (e.absolute && e.alpha != null) {
    deviceHeading = (360 - e.alpha) % 360;
    refreshArrow();
  }
}, true);

window.addEventListener('deviceorientation', e => {
  if (deviceHeading == null && e.alpha != null) {
    deviceHeading = (360 - e.alpha) % 360;
    refreshArrow();
  }
}, true);

// ── Geometry ──────────────────────────────────────────────────────────────────

function bearing(lat1, lon1, lat2, lon2) {
  const r = Math.PI / 180;
  const φ1 = lat1 * r, φ2 = lat2 * r, Δλ = (lon2 - lon1) * r;
  const y = Math.sin(Δλ) * Math.cos(φ2);
  const x = Math.cos(φ1) * Math.sin(φ2) - Math.sin(φ1) * Math.cos(φ2) * Math.cos(Δλ);
  return (Math.atan2(y, x) / r + 360) % 360;
}

function distanceM(lat1, lon1, lat2, lon2) {
  const R = 6371000, r = Math.PI / 180;
  const Δφ = (lat2 - lat1) * r, Δλ = (lon2 - lon1) * r;
  const a = Math.sin(Δφ / 2) ** 2
    + Math.cos(lat1 * r) * Math.cos(lat2 * r) * Math.sin(Δλ / 2) ** 2;
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function fmtDist(m) {
  return m >= 1000 ? `${(m / 1000).toFixed(1)} km` : `${Math.round(m)} m`;
}

// ── Arrow refresh ─────────────────────────────────────────────────────────────

function refreshArrow() {
  if (!stops.length || currentIdx >= stops.length) return;
  const stop = stops[currentIdx];
  if (!stop.lat || !stop.lon) return;

  const distEl = document.getElementById('distance-label');
  const arrowEl = document.getElementById('arrow');

  if (deviceLat == null) {
    arrowEl.style.transform = '';
    distEl.textContent = '—';
    return;
  }

  const d = distanceM(deviceLat, deviceLon, stop.lat, stop.lon);
  distEl.textContent = fmtDist(d);

  // Within throat switch distance → point at address directly
  const throatM = stop.throat_distance_m;
  const target = (throatM != null && d <= throatM + THROAT_SWITCH_M)
    ? { lat: stop.lat, lon: stop.lon }  // point at address
    : { lat: stop.lat, lon: stop.lon }; // same for now; extend to spine in future

  const bear = bearing(deviceLat, deviceLon, target.lat, target.lon);
  const rotation = (deviceHeading != null) ? bear - deviceHeading : bear;
  arrowEl.style.transform = `rotate(${rotation}deg)`;

  updateThroatBar(d, throatM);
}

// ── Throat bar ────────────────────────────────────────────────────────────────

function updateThroatBar(distToStop, throatM) {
  const wrap = document.getElementById('throat-bar-wrap');
  if (throatM == null || throatM === 0) {
    wrap.classList.remove('visible');
    return;
  }
  // Show bar when within 3× throat distance
  const triggerDist = throatM * 3;
  if (distToStop > triggerDist) {
    wrap.classList.remove('visible');
    return;
  }
  wrap.classList.add('visible');
  const pct = Math.min(100, Math.max(0, (distToStop / triggerDist) * 100));
  document.getElementById('throat-bar-fill').style.width = `${pct}%`;
  document.getElementById('throat-bar-dist').textContent =
    `throat in ~${fmtDist(Math.max(0, distToStop - (distToStop - throatM)))}`;
}

// ── Parcel list (draggable rows) ──────────────────────────────────────────────

const DEFAULT_PARCELS = [
  '4 Highfield Road, NR19 2EY',
  '7 Highfield Road, NR19 2EY',
  '12 Highfield Road, NR19 2EY',
  '19 Highfield Road, NR19 2EY',
  '26 Highfield Road, NR19 2EY',
  '33 Highfield Road, NR19 2EY',
  '21 Oakwood Road, NR19 2SS',
  '2 Oakwood Close, NR19 2ST',
  '3 Oakwood Close, NR19 2ST',
  '4 Oakwood Close, NR19 2ST',
  '8 Oakwood Close, NR19 2ST',
  '19 Oakwood Close, NR19 2ST',
  '22 Northgate, NR19 2EU',
  '14 Northgate, NR19 2EU',
  'Weldon Lodge, NR19 2EU',
  'Longfields, NR19 2EU',
];

let _dragEl        = null;
let _lpTimer       = null;

function addParcelRow(text) {
  const list = document.getElementById('parcel-list');
  const row  = document.createElement('div');
  row.className = 'parcel-row';

  const handle = document.createElement('span');
  handle.className   = 'drag-handle';
  handle.textContent = '⠿';

  const inp = document.createElement('input');
  inp.className   = 'parcel-input';
  inp.type        = 'text';
  inp.placeholder = 'Address, Postcode';
  inp.value       = text;

  const del = document.createElement('span');
  del.className   = 'parcel-del';
  del.textContent = '✕';
  del.onclick     = () => row.remove();

  row.append(handle, inp, del);

  // Long-press drag (touch)
  handle.addEventListener('touchstart', e => {
    const touch = e.touches[0];
    _lpTimer = setTimeout(() => {
      _dragEl = row;
      row.classList.add('dragging');
      navigator.vibrate && navigator.vibrate(15);
    }, 350);
  }, { passive: true });

  // Immediate drag (mouse / desktop)
  handle.addEventListener('mousedown', e => {
    e.preventDefault();
    _dragEl = row;
    row.classList.add('dragging');
  });

  list.appendChild(row);
}

function getParcelLines() {
  return Array.from(document.querySelectorAll('#parcel-list .parcel-input'))
    .map(i => i.value.trim()).filter(Boolean);
}

// Global drag move + end
document.addEventListener('touchmove', e => {
  if (!_dragEl) { clearTimeout(_lpTimer); return; }
  e.preventDefault();
  const t      = e.touches[0];
  const target = document.elementFromPoint(t.clientX, t.clientY);
  const trow   = target && target.closest('#parcel-list .parcel-row');
  if (trow && trow !== _dragEl) {
    const mid = trow.getBoundingClientRect().top + trow.getBoundingClientRect().height / 2;
    t.clientY < mid ? trow.before(_dragEl) : trow.after(_dragEl);
  }
}, { passive: false });

document.addEventListener('touchend', () => {
  clearTimeout(_lpTimer); _lpTimer = null;
  if (_dragEl) { _dragEl.classList.remove('dragging'); _dragEl = null; }
});

document.addEventListener('mousemove', e => {
  if (!_dragEl) return;
  const target = document.elementFromPoint(e.clientX, e.clientY);
  const trow   = target && target.closest('#parcel-list .parcel-row');
  if (trow && trow !== _dragEl) {
    const mid = trow.getBoundingClientRect().top + trow.getBoundingClientRect().height / 2;
    e.clientY < mid ? trow.before(_dragEl) : trow.after(_dragEl);
  }
});

document.addEventListener('mouseup', () => {
  if (_dragEl) { _dragEl.classList.remove('dragging'); _dragEl = null; }
});

// Paste import
function togglePaste() {
  const ta  = document.getElementById('parcels');
  const btn = document.getElementById('btn-import-paste');
  const tog = document.getElementById('btn-paste-toggle');
  const show = ta.style.display === 'none';
  ta.style.display  = show ? 'block' : 'none';
  btn.style.display = show ? 'block' : 'none';
  tog.textContent   = show ? 'Paste ▴' : 'Paste ▾';
  if (show) ta.focus();
}

function importPaste() {
  const ta    = document.getElementById('parcels');
  const lines = ta.value.trim().split('\n').map(l => l.trim()).filter(Boolean);
  const list  = document.getElementById('parcel-list');
  list.innerHTML = '';
  lines.forEach(l => addParcelRow(l));
  ta.value = '';
  togglePaste();
}

// ── Postcode normalisation ────────────────────────────────────────────────────

function normalizePostcode(pc) {
  const s = pc.replace(/\s+/g, '').toUpperCase();
  return s.length >= 5 ? s.slice(0, -3) + ' ' + s.slice(-3) : s;
}

function parseParcels(lines) {
  return lines.map(line => {
    const comma = line.lastIndexOf(',');
    if (comma < 0) return null;
    return { addr: line.slice(0, comma).trim(), pc: normalizePostcode(line.slice(comma + 1).trim()) };
  }).filter(Boolean);
}

// ── API ───────────────────────────────────────────────────────────────────────

async function api(path, body) {
  const r = await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const err = await r.json().catch(() => ({ detail: r.statusText }));
    throw new Error(err.detail || r.statusText);
  }
  return r.json();
}

// ── Entry point ───────────────────────────────────────────────────────────────

async function startRoute() {
  const btn   = document.getElementById('btn-optimise');
  const errEl = document.getElementById('setup-error');
  errEl.style.display = 'none';

  const startAddr  = document.getElementById('start-addr').value.trim();
  const startPc    = normalizePostcode(document.getElementById('start-pc').value.trim());
  const finishAddr = document.getElementById('finish-addr').value.trim();
  const finishPc   = normalizePostcode(document.getElementById('finish-pc').value.trim());
  const parcels    = parseParcels(getParcelLines());

  if (!startAddr || !startPc) { showError('Start address and postcode required'); return; }
  if (!parcels.length)         { showError('No valid parcels found'); return; }

  btn.disabled    = true;
  btn.textContent = 'Optimising…';

  try {
    const body = { parcels, start_addr: startAddr, start_pc: startPc };
    if (finishAddr && finishPc) { body.finish_addr = finishAddr; body.finish_pc = finishPc; }
    const data = await api('/api/navigation/optimise', body);

    sessionId  = data.session_id;
    stops      = data.stops;
    totalTime  = data.total_time;
    currentIdx = 0;

    document.getElementById('setup').style.display = 'none';
    document.getElementById('nav').style.display   = 'flex';
    renderStop(0);
  } catch (e) {
    showError(e.message);
  } finally {
    btn.disabled    = false;
    btn.textContent = 'Optimise Route';
  }
}

function showError(msg) {
  const el = document.getElementById('setup-error');
  el.textContent    = msg;
  el.style.display  = 'block';
}

// ── Navigation ────────────────────────────────────────────────────────────────

function goNext() {
  if (currentIdx >= stops.length - 1) { showDone(); return; }
  currentIdx++;
  renderStop(currentIdx);
  document.getElementById('scroll-body').scrollTo({ top: 0, behavior: 'smooth' });
}

function goPrev() {
  if (currentIdx <= 0) return;
  currentIdx--;
  renderStop(currentIdx);
  document.getElementById('scroll-body').scrollTo({ top: 0, behavior: 'smooth' });
}

function jumpTo(idx) {
  currentIdx = idx;
  renderStop(idx);
  document.getElementById('scroll-body').scrollTo({ top: 0, behavior: 'smooth' });
}

// ── Render ────────────────────────────────────────────────────────────────────

function renderStop(idx) {
  const stop  = stops[idx];
  const total = stops.length;

  document.getElementById('prog-label').textContent     = `${idx + 1} / ${total}`;
  document.getElementById('prog-time').textContent      = stop.time_str;
  document.getElementById('progress-fill').style.width  = `${((idx + 1) / total) * 100}%`;
  document.getElementById('ctr-cur').textContent        = idx + 1;
  document.getElementById('ctr-of').textContent         = ` of ${total}`;
  document.getElementById('btn-prev').disabled          = idx === 0;
  document.getElementById('btn-next-stop').textContent  = idx >= total - 1 ? 'Finish ✓' : 'Next ▶';

  if (stop.postcode !== lastMetaPc) {
    lastMetaPc = stop.postcode;
    renderMeta(stop);
    metaOpen = false;
    document.getElementById('meta-header').classList.remove('open');
    document.getElementById('meta-body').classList.remove('visible');
  }

  renderWarnings(stop);

  document.getElementById('drop-badge').textContent = `DROP ${stop.drop}`;
  document.getElementById('stop-addr').textContent  = stop.address;
  document.getElementById('stop-pc').textContent    = stop.postcode;
  document.getElementById('stop-bubble').textContent = stop.bubble || '';
  document.getElementById('stop-time').textContent  = stop.time_str;
  document.getElementById('stop-type').textContent  = stop.prop_type;
  document.getElementById('stop-pkgs').textContent  = `${stop.pkgs} pkg`;

  // Throat bar (static, before GPS)
  const throatWrap = document.getElementById('throat-bar-wrap');
  if (stop.throat_distance_m != null) {
    throatWrap.classList.add('visible');
    document.getElementById('throat-bar-dist').textContent =
      stop.throat_distance_m === 0 ? 'at entry' : `~${stop.throat_distance_m}m`;
    document.getElementById('throat-bar-fill').style.width = '100%';
  } else {
    throatWrap.classList.remove('visible');
  }

  renderUpcoming(idx);
  refreshArrow();
}

function renderWarnings(stop) {
  const m = stop.meta || {};
  const b = [];
  if (m.pattern)                       b.push(`<div class="banner banner-pattern">◈ ${esc(m.pattern.toUpperCase())}</div>`);
  if (stop.throat_distance_m != null)  b.push(`<div class="banner banner-warn">⚠ THROAT ${stop.throat_distance_m === 0 ? '@ entry' : `@ ${stop.throat_distance_m}m`}</div>`);
  if (stop.no_uturn || m.no_uturn)     b.push(`<div class="banner banner-danger">✕ NO U-TURN</div>`);
  if (m.descending)                    b.push(`<div class="banner banner-warn">↓ DESCENDING</div>`);
  if (m.raynham_ride && m.raynham_ride.walk_of_shame)
                                       b.push(`<div class="banner banner-orange">🚶 WALK OF SHAME — on foot only</div>`);
  document.getElementById('warnings').innerHTML = b.join('');
}

function renderMeta(stop) {
  const m = stop.meta || {};
  document.getElementById('meta-pc').textContent      = stop.postcode;
  document.getElementById('meta-streets').textContent = (m.streets && m.streets.length) ? m.streets.join(', ') : '';

  const rows = [];
  if (m.entry && m.entry !== '—')      rows.push(['entry',     m.entry]);
  if (m.exit  && m.exit  !== '—')      rows.push(['exit',      m.exit]);
  if (m.direction && m.direction !== '—') rows.push(['direction', m.direction]);
  if (m.delivery_side)                 rows.push(['side',      m.delivery_side]);
  if (m.throat_label)                  rows.push([m.throat_type === 'functional' ? 'func throat' : 'throat', m.throat_label]);
  if (m.turning_point)                 rows.push(['turn pt',   m.turning_point]);
  if (m.reverse_required && m.reverse_required.length)
                                       rows.push(['reverse',   m.reverse_required.join(' → ')]);
  if (m.prominent_landmark)            rows.push(['landmark',  m.prominent_landmark]);

  let html = rows.map(([k, v]) =>
    `<div class="meta-row"><span class="meta-key">${esc(k)}</span><span class="meta-val">${esc(v)}</span></div>`
  ).join('');

  if (m.internal_order && m.internal_order.length) {
    html += `<div class="meta-order" style="margin-top:6px">` +
      m.internal_order.map(esc).join(' <span style="color:var(--muted)">→</span> ') + '</div>';
  }

  if (m.raynham_ride) {
    const rr = m.raynham_ride;
    html += `<div class="meta-row" style="margin-top:6px"><span class="meta-key">ride</span>` +
      `<span class="meta-val">${esc(rr.flow || rr.intercept || '')}</span></div>`;
  }

  if (m.pattern) {
    const segs = [m.segment_a, m.segment_b, m.segment_c].filter(Boolean);
    if (segs.length) {
      html += `<div style="margin-top:8px;font-size:12px;color:var(--accent)">` +
        segs.map((s, i) => `<div>${'ABC'[i]}: ${esc(s)}</div>`).join('') + '</div>';
    }
  }

  document.getElementById('meta-content').innerHTML = html;
}

function renderUpcoming(idx) {
  const upcoming = stops.slice(idx + 1, idx + 4);
  const el = document.getElementById('upcoming');
  if (!upcoming.length) { el.innerHTML = ''; return; }
  let html = '<div class="upcoming-label">Up next</div>';
  upcoming.forEach((s, i) => {
    html += `<div class="upcoming-item" onclick="jumpTo(${idx + 1 + i})">` +
      `<div class="u-drop">D${s.drop}</div>` +
      `<div><div class="u-addr">${esc(s.address)}</div><div class="u-pc">${esc(s.postcode)}</div></div>` +
      `<div class="u-time">${esc(s.time_str)}</div>` +
      `</div>`;
  });
  el.innerHTML = html;
}

// ── Meta toggle ───────────────────────────────────────────────────────────────

function toggleMeta() {
  metaOpen = !metaOpen;
  document.getElementById('meta-header').classList.toggle('open', metaOpen);
  document.getElementById('meta-body').classList.toggle('visible', metaOpen);
}

// ── Done ──────────────────────────────────────────────────────────────────────

function showDone() {
  document.getElementById('nav').style.display  = 'none';
  document.getElementById('done').style.display = 'flex';
  document.getElementById('done-summary').textContent = `${stops.length} stops · ${totalTime}`;
}

function resetApp() {
  sessionId = null; stops = []; currentIdx = 0; lastMetaPc = null;
  document.getElementById('done').style.display  = 'none';
  document.getElementById('setup').style.display = 'flex';
}

// ── Scan (barcode) ────────────────────────────────────────────────────────────

async function scanBarcode(barcode) {
  if (!sessionId) return null;
  try {
    const data = await api('/api/navigation/scan', { session_id: sessionId, barcode });
    if (data.drop != null) {
      jumpTo(data.drop - 1);
      return data;
    }
  } catch (e) {
    console.warn('scan:', e.message);
  }
  return null;
}

// ── Init ──────────────────────────────────────────────────────────────────────

DEFAULT_PARCELS.forEach(l => addParcelRow(l));

// ── Helpers ───────────────────────────────────────────────────────────────────

function esc(s) {
  if (s == null) return '';
  return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}
