/* Forge Vision UI — vanilla JS, no build step. */
"use strict";

const $ = (id) => document.getElementById(id);
const api = async (path, opts = {}) => {
  const res = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail; } catch (e) { /* keep statusText */ }
    throw new Error(detail);
  }
  return res.json();
};
const fmtHz = (hz) => hz >= 1e9 ? (hz / 1e9).toFixed(3) + " GHz"
  : hz >= 1e6 ? (hz / 1e6).toFixed(2) + " MHz"
  : hz >= 1e3 ? (hz / 1e3).toFixed(1) + " kHz" : hz.toFixed(0) + " Hz";
const fmtBytes = (b) => b >= 1 << 30 ? (b / (1 << 30)).toFixed(1) + " GiB"
  : b >= 1 << 20 ? (b / (1 << 20)).toFixed(1) + " MiB" : (b / 1024).toFixed(0) + " KiB";
const esc = (s) => String(s ?? "").replace(/[&<>"]/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

let STATUS = null;
let selectedExp = null;
let currentScan = null;
let ws = null;

/* ---------- tabs ---------- */
document.querySelectorAll("nav button").forEach((b) => {
  b.onclick = () => {
    document.querySelectorAll("nav button").forEach((x) => x.classList.remove("active"));
    document.querySelectorAll(".tab").forEach((x) => x.classList.remove("active"));
    b.classList.add("active");
    $("tab-" + b.dataset.tab).classList.add("active");
    if (b.dataset.tab === "library") refreshLibrary();
    if (b.dataset.tab === "safety") refreshSafety();
    if (b.dataset.tab === "dashboard") refreshStatus();
    if (b.dataset.tab === "antenna") refreshComponents();
  };
});

/* ---------- global status + TX indicator ---------- */
function setTxIndicator(safety) {
  const el = $("tx-indicator");
  if (safety.tx_active) {
    el.className = "tx-on";
    el.textContent = "TX ACTIVE — " + safety.tx_active_devices.join(", ");
  } else {
    el.className = "tx-off";
    el.textContent = safety.armed ? "TX OFF (armed)" : "TX OFF";
  }
}

async function refreshStatus() {
  try {
    STATUS = await api("/api/status");
  } catch (e) { return; }
  setTxIndicator(STATUS.safety);
  renderDashboard();
  fillSelectors();
}

function renderDashboard() {
  const s = STATUS;
  $("device-list").innerHTML = s.devices.map((d) => `
    <div class="devcard">
      <div><span class="dot ${d.connected ? "on" : "off"}"></span>
        <b>${esc(d.device_id)}</b> <span class="mut">${esc(d.kind)}</span><br>
        <span class="mut">${fmtHz(d.config.center_frequency_hz)} ·
        ${(d.config.sample_rate_hz / 1e6).toFixed(2)} MSPS ·
        RX ${d.config.rx_gain_db} dB · TX ${d.config.tx_gain_db} dB</span></div>
      <div>${d.tx_enabled ? '<span class="tag" style="background:#57120a;color:#ffb4a4">TX</span>' : ""}
        ${d.connected
          ? `<button onclick="devDisconnect('${d.device_id}')">Disconnect</button>`
          : `<button onclick="devConnect('${d.device_id}')">Connect</button>`}</div>
    </div>`).join("");
  $("system-info").innerHTML = `
    <div class="mono">version ${esc(s.version)}
storage: ${fmtBytes(s.storage.experiments_bytes)} experiments · ${fmtBytes(s.storage.disk_free_bytes)} free${s.storage.low_space_warning ? " ⚠ LOW" : ""}
safety: ${s.safety.armed ? "armed by " + esc(s.safety.armed_by) : "disarmed"} · profile ${esc(s.safety.limits.active_profile)}
active scans: ${Object.keys(s.active_scans).length}</div>`;
  $("recent-experiments").innerHTML = s.recent_experiments.map((e) => `
    <div class="expitem" onclick="openLibrary('${e.experiment_id}')">
      <div><b>${esc(e.name)}</b> <span class="tag">${esc(e.kind)}</span>
        <span class="mut">${e.num_segments} seg · ${e.derived.map(esc).join(", ")}</span></div>
      <div class="mut">${new Date(e.started_at * 1000).toLocaleString()} · ${esc(e.status)}</div>
    </div>`).join("") || '<span class="mut">none yet</span>';
}

function fillSelectors() {
  const devs = STATUS.devices.map((d) => d.device_id);
  for (const id of ["live-device", "range-device", "scan-device"]) {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = devs.map((d) => `<option>${esc(d)}</option>`).join("");
    if (devs.includes(cur)) sel.value = cur;
  }
  const wfs = Object.keys(STATUS.waveforms);
  for (const id of ["live-waveform", "range-waveform", "scan-waveform"]) {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = wfs.map((w) => `<option>${esc(w)}</option>`).join("");
    sel.value = wfs.includes(cur) ? cur : "fmcw_bench_56M";
  }
  const media = Object.keys(STATUS.media_presets);
  for (const id of ["range-medium", "scan-medium"]) {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = media.map((m) => `<option>${esc(m)}</option>`).join("");
    if (media.includes(cur)) sel.value = cur;
    else sel.value = id === "scan-medium" ? "soil_dry" : "air";
  }
  const prof = $("freq-profile");
  const profiles = Object.keys(STATUS.safety.limits.frequency_profiles);
  prof.innerHTML = profiles.map((p) => `<option>${esc(p)}</option>`).join("");
  prof.value = STATUS.safety.limits.active_profile;
}

window.devConnect = async (id) => { await api(`/api/devices/${id}/connect`, { method: "POST" }); refreshStatus(); };
window.devDisconnect = async (id) => { await api(`/api/devices/${id}/disconnect`, { method: "POST" }); refreshStatus(); };

$("estop").onclick = async () => {
  const r = await api("/api/safety/stop", { method: "POST" });
  alert("EMERGENCY STOP\n" + r.results.join("\n"));
  refreshStatus();
};

/* ---------- hardware rescan ---------- */
async function doRescan(uri) {
  $("rescan-status").textContent = "probing…";
  try {
    const r = await api("/api/devices/rescan", { method: "POST", body: { uri } });
    if (!r.driver.available) { $("rescan-status").textContent = r.driver.detail; return; }
    const bits = [];
    if (r.added.length) bits.push("added: " + r.added.map((d) => d.device_id).join(", "));
    if (r.already_present.length) bits.push("already present: " + r.already_present.join(", "));
    r.errors.forEach((e) => bits.push(`${e.uri}: ${e.error}`));
    $("rescan-status").textContent = bits.join(" · ") || "no devices found";
    refreshStatus();
  } catch (e) { $("rescan-status").textContent = "rescan failed: " + e.message; }
}
$("rescan-btn").onclick = () => doRescan("");
$("rescan-uri-btn").onclick = () => doRescan($("rescan-uri").value.trim());

/* ---------- Live RF ---------- */
let waterfallRows = [];

$("live-connect").onclick = async () => {
  const id = $("live-device").value;
  try {
    await api(`/api/devices/${id}/connect`, { method: "POST" });
    $("live-status").textContent = "connected";
    $("live-stream").disabled = false;
    $("live-tx").disabled = false;
    $("live-record").disabled = false;
    refreshStatus();
  } catch (e) { $("live-status").textContent = "error: " + e.message; }
};

$("cfg-apply").onclick = async () => {
  const id = $("live-device").value;
  try {
    await api(`/api/devices/${id}/configure`, { method: "POST", body: {
      center_frequency_hz: parseFloat($("cfg-freq").value) * 1e6,
      sample_rate_hz: parseFloat($("cfg-rate").value) * 1e6,
      rx_bandwidth_hz: parseFloat($("cfg-bw").value) * 1e6,
      rx_gain_db: parseFloat($("cfg-rxgain").value),
      tx_gain_db: parseFloat($("cfg-txgain").value),
    }});
    $("live-status").textContent = "config applied";
  } catch (e) { $("live-status").textContent = "rejected: " + e.message; }
};

$("live-stream").onclick = () => {
  if (ws) { ws.close(); ws = null; $("live-stream").textContent = "Start stream"; return; }
  const id = $("live-device").value;
  ws = new WebSocket(`ws://${location.host}/ws/live?device_id=${id}&fps=6`);
  $("live-stream").textContent = "Stop stream";
  ws.onmessage = (ev) => {
    const f = JSON.parse(ev.data);
    if (f.error) { $("live-status").textContent = f.error; return; }
    setTxIndicator(f.safety);
    drawSpectrum(f.spectrum);
    pushWaterfall(f.spectrum);
    drawTimeDomain(f.iq_preview);
    renderLiveAlerts(f);
    $("live-quality").textContent = JSON.stringify(
      { quality: f.quality, telemetry: f.telemetry, tx_active: f.tx_active }, null, 1);
  };
  ws.onclose = () => { $("live-stream").textContent = "Start stream"; ws = null; };
};

$("live-tx").onclick = async () => {
  const id = $("live-device").value;
  const enable = $("live-tx").textContent.startsWith("Enable");
  try {
    await api(`/api/devices/${id}/tx`, { method: "POST",
      body: { enable, waveform: $("live-waveform").value } });
    $("live-tx").textContent = enable ? "Disable TX" : "Enable TX";
    $("live-status").textContent = enable ? "transmitting" : "tx off";
    refreshStatus();
  } catch (e) {
    $("live-status").textContent = "TX refused: " + e.message;
    alert("Transmit refused:\n" + e.message +
      "\n\nArm the interlock in the Safety tab first (FR-SAF-001).");
  }
};

$("live-record").onclick = async () => {
  const id = $("live-device").value;
  $("live-status").textContent = "recording…";
  try {
    const r = await api("/api/capture", { method: "POST", body: {
      device_id: id, num_samples: 262144, segments: 2,
      name: "live raw capture", waveform: "" } });
    $("live-status").textContent = `saved ${r.experiment_id} (${fmtBytes(r.bytes_estimate)})`;
  } catch (e) { $("live-status").textContent = "record failed: " + e.message; }
};

function renderLiveAlerts(f) {
  const alerts = [];
  if (f.clipped) alerts.push(["err", "Receiver clipping — reduce gain (UX-LIVE-005)"]);
  if (f.loss_events && f.loss_events.length)
    alerts.push(["err", `Sample loss: ${f.loss_events.length} event(s) — recorded, not concealed`]);
  if (f.quality && f.quality.near_clipping) alerts.push(["warn", "Signal near full scale"]);
  $("live-alerts").innerHTML = alerts.map(([c, t]) =>
    `<div class="alert ${c}">${esc(t)}</div>`).join("");
}

function drawSpectrum(sp) {
  const cv = $("spectrum"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  const psd = sp.psd_db, n = psd.length;
  const lo = -110, hi = -20;
  ctx.strokeStyle = "#35c4a2"; ctx.lineWidth = 1.4; ctx.beginPath();
  for (let i = 0; i < n; i++) {
    const x = (i / (n - 1)) * cv.width;
    const y = cv.height - ((psd[i] - lo) / (hi - lo)) * cv.height;
    i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
  }
  ctx.stroke();
  ctx.fillStyle = "#7d8ba0"; ctx.font = "11px monospace";
  const f0 = sp.center_frequency_hz;
  ctx.fillText(fmtHz(f0 + sp.freqs_hz[0]), 4, cv.height - 4);
  ctx.fillText(fmtHz(f0), cv.width / 2 - 30, cv.height - 4);
  ctx.fillText(fmtHz(f0 + sp.freqs_hz[n - 1]), cv.width - 78, cv.height - 4);
}

function pushWaterfall(sp) {
  waterfallRows.push(sp.psd_db);
  if (waterfallRows.length > 130) waterfallRows.shift();
  const cv = $("waterfall"), ctx = cv.getContext("2d");
  const w = cv.width, h = cv.height, rows = waterfallRows.length;
  const img = ctx.createImageData(w, h);
  for (let r = 0; r < rows; r++) {
    const psd = waterfallRows[rows - 1 - r], n = psd.length;
    const y0 = Math.floor((r / 130) * h), y1 = Math.floor(((r + 1) / 130) * h);
    for (let x = 0; x < w; x++) {
      const v = psd[Math.floor((x / w) * n)];
      const [cr, cg, cb] = viridis((v + 110) / 90);
      for (let y = y0; y < y1; y++) {
        const o = (y * w + x) * 4;
        img.data[o] = cr; img.data[o + 1] = cg; img.data[o + 2] = cb; img.data[o + 3] = 255;
      }
    }
  }
  ctx.putImageData(img, 0, 0);
}

function drawTimeDomain(iq) {
  const cv = $("timedom"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  const draw = (arr, color) => {
    ctx.strokeStyle = color; ctx.lineWidth = 1; ctx.beginPath();
    const n = arr.length;
    for (let i = 0; i < n; i++) {
      const x = (i / (n - 1)) * cv.width;
      const y = cv.height / 2 - arr[i] * (cv.height / 2.2);
      i ? ctx.lineTo(x, y) : ctx.moveTo(x, y);
    }
    ctx.stroke();
  };
  draw(iq.i, "#35c4a2"); draw(iq.q, "#4aa3ff");
}

function viridis(t) {
  t = Math.max(0, Math.min(1, t));
  const stops = [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
  const x = t * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(x)), f = x - i;
  return [0,1,2].map((k) => Math.round(stops[i][k] * (1 - f) + stops[i + 1][k] * f));
}

/* ---------- Range Lab ---------- */
let lastProfile = null;

$("range-bg").onclick = async () => {
  const id = $("range-device").value;
  $("range-status").textContent = "capturing background…";
  try {
    await ensureConnected(id);
    const r = await api(`/api/calibration/${id}/background`, { method: "POST",
      body: { waveform: $("range-waveform").value } });
    $("range-status").textContent = "background stored → " + r.experiment_id;
    renderCalBanner(r.calibration);
    refreshStatus();
  } catch (e) { $("range-status").textContent = "failed: " + e.message; rangeTxHint(e); }
};

$("range-run").onclick = async () => {
  const id = $("range-device").value;
  $("range-status").textContent = "running…";
  try {
    await ensureConnected(id);
    const r = await api("/api/range/run", { method: "POST", body: {
      device_id: id,
      waveform: $("range-waveform").value,
      medium: $("range-medium").value,
      chirps: parseInt($("range-chirps").value, 10),
      use_background: $("range-usebg").checked,
      name: "range run (" + $("range-medium").value + ")",
    }});
    $("range-status").textContent = "done → " + r.experiment_id;
    lastProfile = r.range_profile;
    drawRangeProfile(r.range_profile, r.peaks);
    renderPeaks(r.peaks);
    renderCalBanner(r.calibration);
    $("range-quality").textContent = JSON.stringify(
      { quality: r.quality, segment: r.segment, warnings: r.warnings }, null, 1);
    $("range-meta").textContent =
      ` — resolution ${r.range_profile.resolution_m.toFixed(2)} m · ` +
      `${r.range_profile.chirps_averaged} chirps · v=${(r.range_profile.velocity_m_per_s / 1e8).toFixed(2)}e8 m/s`;
    refreshStatus();
  } catch (e) { $("range-status").textContent = "failed: " + e.message; rangeTxHint(e); }
};

function rangeTxHint(e) {
  if (String(e.message).includes("interlock")) {
    alert("Transmit interlock is not armed.\nGo to the Safety tab and arm TX for this session.");
  }
}

async function ensureConnected(id) {
  const dev = STATUS.devices.find((d) => d.device_id === id);
  if (dev && !dev.connected) await api(`/api/devices/${id}/connect`, { method: "POST" });
}

function renderCalBanner(cal) {
  const el = $("range-cal");
  if (cal.valid) {
    el.className = "calbanner cal-ok";
    el.textContent = "Calibration OK — cable delay " +
      (cal.cable_delay_s * 1e9).toFixed(1) + " ns, background applied.";
  } else {
    el.className = "calbanner cal-warn";
    el.innerHTML = "<b>Calibration warnings:</b> " + cal.warnings.map(esc).join(" · ");
  }
}

function drawRangeProfile(p, peaks) {
  const cv = $("range-plot"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  const mags = p.magnitude_db, ranges = p.ranges_m, n = mags.length;
  const lo = Math.min(...mags), hi = Math.max(...mags) + 5;
  const X = (i) => (ranges[i] / ranges[n - 1]) * (cv.width - 50) + 40;
  const Y = (v) => cv.height - 24 - ((v - lo) / (hi - lo)) * (cv.height - 40);
  ctx.strokeStyle = "#1b2331";
  for (let g = 0; g <= 8; g++) {
    const gx = 40 + (g / 8) * (cv.width - 50);
    ctx.beginPath(); ctx.moveTo(gx, 8); ctx.lineTo(gx, cv.height - 24); ctx.stroke();
    ctx.fillStyle = "#7d8ba0"; ctx.font = "11px monospace";
    ctx.fillText((ranges[n - 1] * g / 8).toFixed(1) + " m", gx - 12, cv.height - 8);
  }
  if (p.magnitude_db_raw) {
    ctx.strokeStyle = "#3a4658"; ctx.lineWidth = 1; ctx.beginPath();
    p.magnitude_db_raw.forEach((v, i) =>
      i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
    ctx.stroke();
  }
  ctx.strokeStyle = "#35c4a2"; ctx.lineWidth = 1.6; ctx.beginPath();
  mags.forEach((v, i) => i ? ctx.lineTo(X(i), Y(v)) : ctx.moveTo(X(i), Y(v)));
  ctx.stroke();
  (peaks || []).forEach((pk) => {
    const i = nearestIndex(ranges, pk.range_m);
    ctx.fillStyle = "#e0a52e";
    ctx.beginPath(); ctx.arc(X(i), Y(mags[i]), 4, 0, 7); ctx.fill();
    // uncertainty interval bar (UX: confidence is visible)
    const iLo = nearestIndex(ranges, pk.range_interval_m[0]);
    const iHi = nearestIndex(ranges, pk.range_interval_m[1]);
    ctx.strokeStyle = "#e0a52e88"; ctx.lineWidth = 3;
    ctx.beginPath(); ctx.moveTo(X(iLo), Y(mags[i]) - 10); ctx.lineTo(X(iHi), Y(mags[i]) - 10); ctx.stroke();
    ctx.fillStyle = "#e8c96a"; ctx.font = "11px monospace";
    ctx.fillText(pk.range_m.toFixed(1) + " m", X(i) - 14, Y(mags[i]) - 16);
  });
}
const nearestIndex = (arr, v) => {
  let best = 0, bd = Infinity;
  for (let i = 0; i < arr.length; i++) {
    const d = Math.abs(arr[i] - v);
    if (d < bd) { bd = d; best = i; }
  }
  return best;
};

function renderPeaks(peaks) {
  $("peak-table").querySelector("tbody").innerHTML = (peaks || []).map((p) => `
    <tr><td><b>${p.range_m.toFixed(2)} m</b></td>
    <td>${p.range_interval_m[0].toFixed(2)}–${p.range_interval_m[1].toFixed(2)} m</td>
    <td>${(p.measured_delay_s * 1e9).toFixed(1)} ns</td>
    <td>${p.power_db.toFixed(1)} dB</td><td>${p.snr_db.toFixed(1)} dB</td>
    <td>${p.width_m.toFixed(2)} m</td>
    <td><span class="tag">${esc(p.confidence.overall)}</span>${p.suspected_leakage
      ? ' <span class="tag" style="background:#3a2a10;color:#e8c96a">TX leakage?</span>' : ""}</td></tr>`).join("")
    || '<tr><td colspan="7" class="mut">no peaks above threshold</td></tr>';
}

/* scene editor */
let sceneTargets = [
  { kind: "plate", range_m: 8.0, amplitude: 0.08, label: "metal plate" },
  { kind: "plate", range_m: 14.0, amplitude: 0.08, label: "far wall" },
];
function renderScene() {
  $("scene-targets").innerHTML = sceneTargets.map((t, i) => `
    <div class="scene-target">
      <span class="mut">#${i + 1}</span>
      range <input type="number" step="0.5" value="${t.range_m}"
        onchange="sceneTargets[${i}].range_m=parseFloat(this.value)"> m
      amp <input type="number" step="0.01" value="${t.amplitude}"
        onchange="sceneTargets[${i}].amplitude=parseFloat(this.value)">
      <button onclick="sceneTargets.splice(${i},1);renderScene()">✕</button>
    </div>`).join("");
}
window.sceneTargets = sceneTargets;
$("scene-add").onclick = () => { sceneTargets.push({ kind: "plate", range_m: 8, amplitude: 0.03, label: "target" }); renderScene(); };
$("scene-apply").onclick = async () => {
  const id = $("range-device").value;
  try {
    await api(`/api/sim/${id}/scene`, { method: "POST", body: { targets: sceneTargets } });
    $("range-status").textContent = "scene applied";
  } catch (e) { $("range-status").textContent = e.message; }
};
renderScene();

/* ---------- Scan Studio ---------- */
$("scan-scene").onclick = async () => {
  const id = $("scan-device").value;
  try {
    await api(`/api/sim/${id}/scene`, { method: "POST", body: { preset: "scan" } });
    $("scan-status").textContent = "buried-target scene loaded (layer @0.9 m, targets @2.2 m and 3.6 m depth)";
  } catch (e) { $("scan-status").textContent = e.message; }
};

$("scan-begin").onclick = async () => {
  const id = $("scan-device").value;
  try {
    await ensureConnected(id);
    const r = await api("/api/scan/start", { method: "POST", body: {
      device_id: id,
      plan: {
        start_m: parseFloat($("scan-start").value),
        end_m: parseFloat($("scan-end").value),
        step_m: parseFloat($("scan-step").value),
        medium: $("scan-medium").value,
        waveform: $("scan-waveform").value,
        chirps: 4,
      }}});
    currentScan = { id: r.scan_id, positions: r.positions_m, next: 0 };
    $("scan-status").textContent = `scan ${r.scan_id} — ${r.positions_m.length} points`;
    $("scan-next").disabled = false; $("scan-auto").disabled = false;
    $("scan-finalize").disabled = false;
    $("scan-resume-id").value = r.scan_id;
    loadAnnotations();
  } catch (e) { $("scan-status").textContent = "failed: " + e.message; }
};

async function captureNext() {
  if (!currentScan) return false;
  const st = await api(`/api/scan/${currentScan.id}/render`);
  const pending = st.status ? st.status.pending : null;
  if (!pending || !pending.length) { $("scan-status").textContent = "scan complete"; return false; }
  const x = pending[0];
  try {
    const r = await api(`/api/scan/${currentScan.id}/point`, { method: "POST", body: { x_m: x } });
    if (!r.accepted) {
      $("scan-status").textContent =
        `point ${x} m REJECTED: ${r.gate_failures.join(", ")} — override available`;
      if (confirm(`Quality gate failed at ${x} m:\n${r.gate_failures.join("\n")}\n\nCapture anyway (operator override)?`)) {
        await api(`/api/scan/${currentScan.id}/point`, { method: "POST",
          body: { x_m: x, operator_override: true } });
      } else return false;
    }
    $("scan-status").textContent = `captured ${x.toFixed(2)} m`;
    await renderBScan();
    return true;
  } catch (e) { $("scan-status").textContent = "failed: " + e.message; rangeTxHint(e); return false; }
}

$("scan-next").onclick = captureNext;
$("scan-auto").onclick = async () => {
  $("scan-auto").disabled = true;
  let more = true;
  while (more) more = await captureNext();
  $("scan-auto").disabled = false;
};
$("scan-finalize").onclick = async () => {
  const r = await api(`/api/scan/${currentScan.id}/finalize`, { method: "POST" });
  $("scan-status").textContent = `finalized as '${r.status}'`;
  refreshStatus();
};
$("scan-resume").onclick = async () => {
  const id = $("scan-resume-id").value.trim();
  if (!id) return;
  try {
    const r = await api(`/api/scan/${id}/resume`, { method: "POST" });
    currentScan = { id, positions: [], next: 0 };
    $("scan-status").textContent =
      `resumed — ${r.status.completed_points}/${r.status.total_points} points done`;
    $("scan-next").disabled = false; $("scan-auto").disabled = false;
    $("scan-finalize").disabled = false;
    await renderBScan(); loadAnnotations();
  } catch (e) { $("scan-status").textContent = "resume failed: " + e.message; }
};
$("scan-interp").onchange = renderBScan;
$("scan-clutter").onchange = renderBScan;
$("scan-qual").onchange = renderBScan;

async function renderBScan() {
  if (!currentScan) return;
  const img = await api(`/api/scan/${currentScan.id}/render?interpolate=${$("scan-interp").checked}&remove_mean=${$("scan-clutter").checked}`);
  if (!img.ranges_m || !img.ranges_m.length) {
    const s = img.status;
    if (s) $("scan-progress").textContent = `${s.completed_points}/${s.total_points} points`;
    return;
  }
  const cv = $("bscan"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  const cols = img.magnitude_db.length, bins = img.ranges_m.length;
  const mLeft = 56, mBottom = 30;
  const cw = (cv.width - mLeft) / cols, chh = (cv.height - mBottom) / bins;
  let lo = Infinity, hi = -Infinity;
  img.magnitude_db.forEach((col) => col && col.forEach((v) => {
    if (v !== null) { lo = Math.min(lo, v); hi = Math.max(hi, v); } }));
  if (!isFinite(lo)) return;
  lo = Math.max(lo, hi - 40);   // 40 dB display span keeps targets prominent
  for (let c = 0; c < cols; c++) {
    const col = img.magnitude_db[c];
    for (let b = 0; b < bins; b++) {
      const v = col ? col[b] : null;
      if (v === null || v === undefined) { ctx.fillStyle = "#141820"; }
      else {
        const [r, g, bl] = viridis((v - lo) / (hi - lo + 1e-9));
        ctx.fillStyle = img.inferred_columns[c]
          ? `rgba(${r},${g},${bl},0.45)` : `rgb(${r},${g},${bl})`;
      }
      ctx.fillRect(mLeft + c * cw, b * chh, Math.ceil(cw), Math.ceil(chh));
    }
    if (img.inferred_columns[c]) {
      ctx.fillStyle = "#e0a52e"; ctx.font = "10px monospace";
      ctx.fillText("~", mLeft + c * cw + cw / 2 - 3, 10);
    }
    if ($("scan-qual").checked) {
      const snr = img.quality.snr_db[c];
      if (snr !== null && snr !== undefined) {
        ctx.fillStyle = snr < 10 ? "#e0492e" : snr < 20 ? "#e0a52e" : "#35c4a2";
        ctx.fillRect(mLeft + c * cw, cv.height - mBottom + 2, Math.ceil(cw) - 1, 5);
      }
      if (img.quality.clipped[c]) {
        ctx.fillStyle = "#e0492e";
        ctx.fillRect(mLeft + c * cw, cv.height - mBottom + 9, Math.ceil(cw) - 1, 3);
      }
    }
  }
  ctx.fillStyle = "#7d8ba0"; ctx.font = "11px monospace";
  for (let g = 0; g <= 5; g++) {
    const r = img.ranges_m[Math.floor((bins - 1) * g / 5)];
    ctx.fillText(r.toFixed(1) + " m", 4, (g / 5) * (cv.height - mBottom - 8) + 12);
  }
  for (let g = 0; g <= 6; g++) {
    const p = img.positions_m[Math.floor((cols - 1) * g / 6)];
    ctx.fillText(p.toFixed(1) + " m", mLeft + (g / 6) * (cv.width - mLeft - 40),
                 cv.height - 6);
  }
  const s = img.status;
  $("scan-progress").textContent =
    `${s.completed_points}/${s.total_points} points · low quality: ` +
    `${s.low_quality.length ? s.low_quality.map((x) => x.toFixed(1)).join(", ") + " m" : "none"}` +
    ` · resolution ${img.resolution_m ? img.resolution_m.toFixed(2) : "?"} m (in medium)`;
  $("scan-meta").textContent = ` — ${currentScan.id}`;
}

$("ann-add").onclick = async () => {
  if (!currentScan) return;
  await api(`/api/experiments/${currentScan.id}/annotate`, { method: "POST", body: {
    type: "note", text: $("ann-text").value,
    x_m: parseFloat($("ann-x").value) || null,
    depth_m: parseFloat($("ann-d").value) || null,
  }});
  $("ann-text").value = "";
  loadAnnotations();
};
async function loadAnnotations() {
  if (!currentScan) return;
  const m = await api(`/api/experiments/${currentScan.id}`);
  $("ann-list").innerHTML = (m.annotations || []).map((a) => `
    <div class="expitem"><div>${esc(a.text)}
      ${a.x_m != null ? `<span class="tag">x=${a.x_m} m</span>` : ""}
      ${a.depth_m != null ? `<span class="tag">d=${a.depth_m} m</span>` : ""}</div>
      <div class="mut">${new Date(a.created_at * 1000).toLocaleTimeString()}</div></div>`)
    .join("") || '<span class="mut">no annotations</span>';
}

/* ---------- Experiment Library ---------- */
async function refreshLibrary() {
  const list = await api(`/api/experiments?query=${encodeURIComponent($("lib-query").value)}` +
    `&kind=${$("lib-kind").value}`);
  $("lib-list").innerHTML = list.map((e) => `
    <div class="expitem ${selectedExp === e.experiment_id ? "sel" : ""}"
         onclick="openLibrary('${e.experiment_id}')">
      <div><b>${esc(e.name)}</b> <span class="tag">${esc(e.kind)}</span><br>
        <span class="mut">${esc(e.experiment_id)} · ${e.num_segments} seg ·
        ${e.derived.map(esc).join(", ") || "no derived"}</span></div>
      <div class="mut">${esc(e.status)}</div>
    </div>`).join("") || '<span class="mut">no experiments</span>';
}
$("lib-refresh").onclick = refreshLibrary;

window.openLibrary = async (id) => {
  selectedExp = id;
  document.querySelector('nav button[data-tab="library"]').click();
  const m = await api(`/api/experiments/${id}`);
  $("lib-detail").textContent = JSON.stringify(m, null, 1);
  $("lib-replay").disabled = false;
  $("lib-verify").disabled = false;
  const ex = $("lib-export");
  ex.classList.remove("disabled");
  ex.href = `/api/experiments/${id}/export`;
  refreshLibrary();
};

$("lib-replay").onclick = async () => {
  if (!selectedExp) return;
  $("lib-detail").textContent = "replaying (reprocessing raw data without hardware)…";
  try {
    const r = await api(`/api/experiments/${selectedExp}/replay`, { method: "POST", body: {} });
    $("lib-detail").textContent = "REPLAY RESULT (stored as " + r.derived_name + ")\n\n" +
      JSON.stringify({ peaks: r.peaks, quality: r.quality,
                       processing: r.processing }, null, 1);
  } catch (e) { $("lib-detail").textContent = "replay failed: " + e.message; }
};
$("lib-verify").onclick = async () => {
  if (!selectedExp) return;
  const r = await api(`/api/experiments/${selectedExp}/verify`);
  alert(r.ok ? "Integrity OK — all checksums match."
    : "INTEGRITY FAILURE\ncorrupt: " + r.corrupt.join(", ") + "\nmissing: " + r.missing.join(", "));
};

/* ---------- Safety tab ---------- */
async function refreshSafety() {
  const s = await api("/api/status");
  STATUS = s;
  $("safety-state").textContent = JSON.stringify(s.safety, null, 1);
  const lim = s.safety.limits;
  $("limits-view").innerHTML = `<pre class="mono">max amplitude      ${lim.max_amplitude}
max duty cycle     ${lim.max_duty_cycle}
max tx gain        ${lim.max_tx_gain_db} dB
frequency range    ${fmtHz(lim.min_frequency_hz)} – ${fmtHz(lim.max_frequency_hz)}
active profile     ${esc(lim.active_profile)}</pre>`;
  const audit = await api("/api/safety/audit?n=100");
  $("audit-log").textContent = audit.map((a) =>
    `${new Date(a.t * 1000).toLocaleTimeString()}  ${a.event}  ` +
    JSON.stringify(Object.fromEntries(Object.entries(a).filter(([k]) => !["t", "event"].includes(k))))
  ).reverse().join("\n") || "no events";
  setTxIndicator(s.safety);
}

$("arm-btn").onclick = async () => {
  if (!$("arm-ack").checked) { alert("You must confirm the safety acknowledgement."); return; }
  try {
    await api("/api/safety/arm", { method: "POST", body: {
      operator: $("arm-operator").value,
      acknowledgement: "operator confirmed antennas/loads safe and authorized" } });
    refreshSafety();
  } catch (e) { alert(e.message); }
};
$("disarm-btn").onclick = async () => { await api("/api/safety/disarm", { method: "POST" }); refreshSafety(); };
$("freq-profile").onchange = async () => {
  await api("/api/safety/profile", { method: "POST", body: { profile: $("freq-profile").value } });
  refreshSafety();
};

/* ---------- Antenna Lab ---------- */
let selectedComp = null;
let pinnedComp = null;          // second trace for comparison
const TRACE_COLORS = ["#35c4a2", "#4aa3ff"];

async function refreshComponents() {
  const list = await api("/api/components");
  $("comp-list").innerHTML = list.map((c) => `
    <div class="expitem ${selectedComp === c.component_id ? "sel" : ""}"
         onclick="openComponent('${c.component_id}')">
      <div><b>${esc(c.name)}</b> <span class="tag">${esc(c.kind)}</span>
        ${c.connector ? `<span class="tag">${esc(c.connector)}</span>` : ""}<br>
        <span class="mut">${esc(c.claimed_band || "no claimed band")}
        ${c.has_vna ? " · VNA ✓" : " · no VNA data"}</span></div>
      <div class="mut">${c.best_match
        ? "best " + fmtHz(c.best_match.freq_hz) + " @ VSWR " + c.best_match.vswr
        : ""}</div>
    </div>`).join("") || '<span class="mut">no components yet — add your antennas and cables</span>';
}

$("comp-add").onclick = async () => {
  const name = $("comp-name").value.trim();
  if (!name) { alert("component needs a name"); return; }
  await api("/api/components", { method: "POST", body: {
    kind: $("comp-kind").value, name,
    connector: $("comp-connector").value.trim(),
    claimed_band: $("comp-band").value.trim() } });
  $("comp-name").value = "";
  refreshComponents();
};

window.openComponent = async (id) => {
  selectedComp = id;
  $("vna-upload").disabled = false;
  $("comp-delete").disabled = false;
  const c = await api(`/api/components/${id}`);
  const meta = { ...c };
  if (meta.vna) meta.vna = { filename: meta.vna.filename, ports: meta.vna.ports,
    points: meta.vna.freqs_hz.length, best_match: meta.vna.analysis.best_match };
  $("comp-detail").textContent = JSON.stringify(meta, null, 1);
  renderBands(c);
  drawVnaPlot(c, pinnedComp);
  refreshComponents();
};

$("comp-pin").onchange = async () => {
  if ($("comp-pin").checked && selectedComp) {
    pinnedComp = await api(`/api/components/${selectedComp}`);
    $("antenna-meta").textContent = ` — pinned: ${pinnedComp.name}`;
  } else {
    pinnedComp = null;
    $("antenna-meta").textContent = "";
  }
};

$("comp-delete").onclick = async () => {
  if (!selectedComp || !confirm("Delete this component?")) return;
  await api(`/api/components/${selectedComp}/delete`, { method: "POST" });
  selectedComp = null;
  $("comp-detail").textContent = "select a component";
  $("comp-bands").innerHTML = "";
  refreshComponents();
};

$("vna-upload").onclick = async () => {
  const input = $("vna-file");
  if (!selectedComp || !input.files.length) { alert("choose a .s1p/.s2p file first"); return; }
  const fd = new FormData();
  fd.append("file", input.files[0]);
  const res = await fetch(`/api/components/${selectedComp}/vna`, { method: "POST", body: fd });
  if (!res.ok) { alert("import failed: " + (await res.json()).detail); return; }
  openComponent(selectedComp);
};

function renderBands(c) {
  if (!c.vna) { $("comp-bands").innerHTML = ""; return; }
  const chips = c.vna.analysis.bands.map((b) => {
    const color = b.rating === "recommended" ? "#12241d;border:1px solid #1f4a38;color:#86dfc0"
      : b.rating === "marginal" ? "#2b2410;border:1px solid #5c4c1c;color:#e8c96a"
      : "#1c1116;border:1px solid #3a2028;color:#a06070";
    return `<span class="tag" style="background:${color}">
      ${fmtHz(b.start_hz)}–${fmtHz(b.stop_hz)} ${esc(b.rating)} (VSWR≥${b.min_vswr})</span>`;
  }).join(" ");
  $("comp-bands").innerHTML = `<p>${chips}</p>`;
}

function drawVnaPlot(c, pinned) {
  const cv = $("vna-plot"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  const traces = [c, pinned].filter((t) => t && t.vna);
  if (!traces.length) {
    ctx.fillStyle = "#7d8ba0"; ctx.font = "13px monospace";
    ctx.fillText("no VNA data imported for this component", 30, 40);
    return;
  }
  const fLo = Math.min(...traces.map((t) => t.vna.freqs_hz[0]));
  const fHi = Math.max(...traces.map((t) => t.vna.freqs_hz.at(-1)));
  const mLeft = 52, mRight = 52, mBottom = 26, mTop = 8;
  const X = (f) => mLeft + ((f - fLo) / (fHi - fLo)) * (cv.width - mLeft - mRight);
  const s11Lo = -40, s11Hi = 0;
  const Y1 = (db) => mTop + (1 - (Math.max(db, s11Lo) - s11Lo) / (s11Hi - s11Lo))
    * (cv.height - mTop - mBottom);
  const vswrLo = 1, vswrHi = 10;
  const Y2 = (v) => mTop + (1 - (Math.min(v, vswrHi) - vswrLo) / (vswrHi - vswrLo))
    * (cv.height - mTop - mBottom);

  ctx.strokeStyle = "#1b2331"; ctx.fillStyle = "#7d8ba0"; ctx.font = "11px monospace";
  for (let g = 0; g <= 8; g++) {
    const gx = mLeft + (g / 8) * (cv.width - mLeft - mRight);
    ctx.beginPath(); ctx.moveTo(gx, mTop); ctx.lineTo(gx, cv.height - mBottom); ctx.stroke();
    ctx.fillText(fmtHz(fLo + (g / 8) * (fHi - fLo)), gx - 24, cv.height - 8);
  }
  for (let db = s11Lo; db <= s11Hi; db += 10) {
    ctx.fillText(db + " dB", 4, Y1(db) + 4);
  }
  for (const v of [1, 2, 3, 5, 10]) {
    ctx.fillText("VSWR " + v, cv.width - mRight + 3, Y2(v) + 4);
  }
  // VSWR=2 guide line (recommended threshold)
  ctx.strokeStyle = "#2c4a3a"; ctx.setLineDash([2, 4]);
  ctx.beginPath(); ctx.moveTo(mLeft, Y2(2)); ctx.lineTo(cv.width - mRight, Y2(2)); ctx.stroke();
  ctx.setLineDash([]);

  traces.forEach((t, ti) => {
    const { freqs_hz, s11_db, vswr } = t.vna;
    ctx.strokeStyle = TRACE_COLORS[ti]; ctx.lineWidth = 1.6; ctx.beginPath();
    freqs_hz.forEach((f, i) =>
      i ? ctx.lineTo(X(f), Y1(s11_db[i])) : ctx.moveTo(X(f), Y1(s11_db[i])));
    ctx.stroke();
    ctx.setLineDash([5, 4]); ctx.lineWidth = 1.1; ctx.beginPath();
    freqs_hz.forEach((f, i) =>
      i ? ctx.lineTo(X(f), Y2(vswr[i])) : ctx.moveTo(X(f), Y2(vswr[i])));
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = TRACE_COLORS[ti];
    ctx.fillText(t.name, mLeft + 8, mTop + 14 + ti * 14);
  });
}

/* ---------- boot ---------- */
refreshStatus();
setInterval(() => { if (!ws) refreshStatus(); }, 5000);
