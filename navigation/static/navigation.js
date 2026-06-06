'use strict';

let sessionId   = null;
let stops       = [];
let currentIdx  = 0;
let totalTime   = '';
let lastMetaPc  = null;
let metaOpen    = false;

// ── Manifest parsing ─────────────────────────────────────────────────────────

function parseParcels(raw) {
  return raw.trim().split('\n')
    .map(l => l.trim()).filter(Boolean)
    .map(line => {
      const comma = line.lastIndexOf(',');
      if (comma < 0) return null;
      const addr = line.slice(0, comma).trim();
      const pc   = line.slice(comma + 1).trim().toUpperCase();
      return addr && pc ? { addr, pc } : null;
    })
    .filter(Boolean);
}

// ── API calls ─────────────────────────────────────────────────────────────────

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
  const startPc    = document.getElementById('start-pc').value.trim().toUpperCase();
  const finishAddr = document.getElementById('finish-addr').value.trim();
  const finishPc   = document.getElementById('finish-pc').value.trim().toUpperCase();
  const parcels    = parseParcels(document.getElementById('parcels').value);

  if (!startAddr || !startPc) {
    showError('Start address and postcode required'); return;
  }
  if (parcels.length === 0) {
    showError('No valid parcels found'); return;
  }

  btn.disabled = true;
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
    btn.disabled = false;
    btn.textContent = 'Optimise Route';
  }
}

function showError(msg) {
  const el = document.getElementById('setup-error');
  el.textContent = msg;
  el.style.display = 'block';
}

// ── Navigation controls ───────────────────────────────────────────────────────

function goNext() {
  if (currentIdx >= stops.length - 1) {
    showDone(); return;
  }
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
  const stop = stops[idx];
  const total = stops.length;

  // Progress
  document.getElementById('prog-label').textContent = `${idx + 1} / ${total}`;
  document.getElementById('prog-time').textContent  = stop.time_str;
  document.getElementById('progress-fill').style.width = `${((idx + 1) / total) * 100}%`;
  document.getElementById('ctr-cur').textContent  = idx + 1;
  document.getElementById('ctr-of').textContent   = ` of ${total}`;

  document.getElementById('btn-prev').disabled = idx === 0;
  document.getElementById('btn-next-stop').textContent = idx >= total - 1 ? 'Finish ✓' : 'Next ▶';

  // PC meta (only re-render if postcode changed)
  if (stop.postcode !== lastMetaPc) {
    lastMetaPc = stop.postcode;
    renderMeta(stop);
    metaOpen = false;
    document.getElementById('meta-header').classList.remove('open');
    document.getElementById('meta-body').classList.remove('visible');
  }

  // Warnings
  renderWarnings(stop);

  // Stop card
  document.getElementById('stop-addr').textContent = stop.address;
  document.getElementById('stop-pc').textContent   = stop.postcode;
  document.getElementById('stop-time').textContent = stop.time_str;
  document.getElementById('stop-type').textContent = stop.prop_type;
  document.getElementById('stop-pkgs').textContent = `${stop.pkgs} pkg`;

  // Upcoming
  renderUpcoming(idx);
}

function renderMeta(stop) {
  const m = stop.meta || {};
  document.getElementById('meta-pc').textContent = stop.postcode;
  document.getElementById('meta-streets').textContent =
    (m.streets && m.streets.length) ? m.streets.join(', ') : '';

  const rows = [];

  if (m.entry && m.entry !== '—') rows.push(['entry', m.entry]);
  if (m.exit  && m.exit  !== '—') rows.push(['exit',  m.exit]);
  if (m.direction && m.direction !== '—') rows.push(['direction', m.direction]);
  if (m.delivery_side) rows.push(['side', m.delivery_side]);
  if (m.throat_label) rows.push([m.throat_type === 'functional' ? 'func throat' : 'throat', m.throat_label]);
  if (m.turning_point) rows.push(['turn pt', m.turning_point]);
  if (m.reverse_required && m.reverse_required.length)
    rows.push(['reverse', m.reverse_required.join(' → ')]);
  if (m.prominent_landmark) rows.push(['landmark', m.prominent_landmark]);

  let html = rows.map(([k, v]) =>
    `<div class="meta-row"><span class="meta-key">${esc(k)}</span><span class="meta-val">${esc(v)}</span></div>`
  ).join('');

  if (m.internal_order && m.internal_order.length) {
    html += `<div class="meta-order" style="margin-top:6px">` +
      m.internal_order.map(esc).join(' <span style="color:var(--muted)">→</span> ') +
      '</div>';
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

function renderWarnings(stop) {
  const m = stop.meta || {};
  const banners = [];

  if (m.pattern) {
    banners.push(`<div class="banner banner-pattern">◈ ${esc(m.pattern.toUpperCase())}</div>`);
  }
  if (stop.throat) {
    banners.push(`<div class="banner banner-warn">⚠ THROAT @ ${esc(stop.throat)}</div>`);
  }
  if (stop.no_uturn || m.no_uturn) {
    banners.push(`<div class="banner banner-danger">✕ NO U-TURN</div>`);
  }
  if (m.descending) {
    banners.push(`<div class="banner banner-warn">↓ DESCENDING</div>`);
  }
  if (m.raynham_ride && m.raynham_ride.walk_of_shame) {
    banners.push(`<div class="banner banner-orange">🚶 WALK OF SHAME — on foot only</div>`);
  }

  document.getElementById('warnings').innerHTML = banners.join('');
}

function renderUpcoming(idx) {
  const upcoming = stops.slice(idx + 1, idx + 4);
  const el = document.getElementById('upcoming');
  if (!upcoming.length) {
    el.innerHTML = '';
    return;
  }
  let html = '<div class="upcoming-label">Up next</div>';
  upcoming.forEach((s, i) => {
    html += `<div class="upcoming-item" onclick="jumpTo(${idx + 1 + i})">` +
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

// ── Done screen ───────────────────────────────────────────────────────────────

function showDone() {
  document.getElementById('nav').style.display  = 'none';
  document.getElementById('done').style.display = 'flex';
  document.getElementById('done-summary').textContent =
    `${stops.length} stops · ${totalTime}`;
}

function resetApp() {
  sessionId  = null;
  stops      = [];
  currentIdx = 0;
  lastMetaPc = null;
  document.getElementById('done').style.display  = 'none';
  document.getElementById('setup').style.display = 'flex';
}

// ── Scan ──────────────────────────────────────────────────────────────────────

async function scanQuery(query) {
  if (!sessionId) return [];
  const data = await api('/api/navigation/scan', { session_id: sessionId, query });
  return data.matches || [];
}

// ── Helpers ───────────────────────────────────────────────────────────────────

function esc(s) {
  if (s == null) return '';
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}
