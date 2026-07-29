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
    if (b.dataset.tab === "world") refreshSites();
    if (b.dataset.tab === "sage") refreshSage();
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
        RX ${d.config.rx_gain_db} dB · TX ${d.config.tx_gain_db} dB<br>
        tuning ${fmtHz(d.capabilities.min_frequency)}–${fmtHz(d.capabilities.max_frequency)} ·
        max BW ${(d.capabilities.max_bandwidth / 1e6).toFixed(0)} MHz ·
        waveforms: ${(d.compatible_waveforms || []).map(esc).join(", ") || "none"}
        ${(d.capability_notes || []).map((n) => "<br>" + esc(n)).join("")}</span></div>
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
  // waveform lists are per-device: a stock AD9363 Pluto (20 MHz) cannot
  // transmit the 56 MHz sweeps, so never offer them for that radio
  for (const [wfId, devId] of [["live-waveform", "live-device"],
                               ["range-waveform", "range-device"],
                               ["scan-waveform", "scan-device"]]) {
    const dev = STATUS.devices.find((d) => d.device_id === $(devId).value);
    const wfs = (dev && dev.compatible_waveforms) || Object.keys(STATUS.waveforms);
    const sel = $(wfId);
    const cur = sel.value;
    sel.innerHTML = wfs.map((w) => `<option>${esc(w)}</option>`).join("");
    sel.value = wfs.includes(cur) ? cur
      : (wfs.includes("fmcw_bench_56M") ? "fmcw_bench_56M" : wfs[0] || "");
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

["live-device", "range-device", "scan-device"].forEach((id) =>
  $(id).addEventListener("change", () => { if (STATUS) fillSelectors(); }));

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
  renderChecklist(s.safety.checklist);
  const audit = await api("/api/safety/audit?n=100");
  $("audit-log").textContent = audit.map((a) =>
    `${new Date(a.t * 1000).toLocaleTimeString()}  ${a.event}  ` +
    JSON.stringify(Object.fromEntries(Object.entries(a).filter(([k]) => !["t", "event"].includes(k))))
  ).reverse().join("\n") || "no events";
  setTxIndicator(s.safety);
}

function renderChecklist(cl) {
  $("checklist").innerHTML = cl.items.map((i) => `
    <div style="margin-bottom:7px">
      <label><input type="checkbox" data-check="${esc(i.id)}"
        ${i.confirmed ? "checked" : ""}>
      ${esc(i.text)} ${i.required
        ? '<span class="tag" style="background:#2d1410;color:#f0a08e">required</span>'
        : '<span class="tag">advisory</span>'}</label>
    </div>`).join("");
  $("checklist").querySelectorAll("input[data-check]").forEach((box) => {
    box.onchange = async () => {
      await api("/api/safety/checklist", { method: "POST",
        body: { id: box.dataset.check, confirmed: box.checked } });
      refreshSafety();
    };
  });
  $("arm-btn").disabled = !cl.complete;
  $("arm-btn").title = cl.complete ? ""
    : "complete the required pre-transmit checks first";
}

$("checklist-reset").onclick = async () => {
  await api("/api/safety/checklist", { method: "POST", body: { reset: true } });
  refreshSafety();
};

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

/* ---------- Band survey (receive only) ---------- */
$("sv-run").onclick = async () => {
  const id = $("live-device").value;
  $("sv-status").textContent = "sweeping… (receive only, no transmission)";
  try {
    await ensureConnected(id);
    const r = await api("/api/survey", { method: "POST", body: {
      device_id: id,
      start_hz: parseFloat($("sv-start").value) * 1e6,
      stop_hz: parseFloat($("sv-stop").value) * 1e6,
      step_hz: parseFloat($("sv-step").value) * 1e6,
      rx_gain_db: parseFloat($("sv-gain").value),
    }});
    drawSurvey(r);
    $("sv-status").textContent = "saved → " + r.experiment_id;
    $("sv-summary").innerHTML =
      `median noise floor ${r.median_noise_floor_dbfs} dBFS · ` +
      `quietest <b>${fmtHz(r.quietest.center_hz)}</b> (peak ${r.quietest.peak_dbfs} dBFS) · ` +
      `busiest <b>${fmtHz(r.busiest.center_hz)}</b> (peak ${r.busiest.peak_dbfs} dBFS)`;
    refreshStatus();
  } catch (e) { $("sv-status").textContent = "survey failed: " + e.message; }
};

function drawSurvey(r) {
  const cv = $("sv-plot"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  const pts = r.points.filter((p) => p.peak_dbfs !== null);
  if (!pts.length) return;
  const mL = 48, mB = 26, mT = 10;
  const lo = Math.min(...pts.map((p) => p.noise_floor_dbfs)) - 5;
  const hi = Math.max(...pts.map((p) => p.peak_dbfs)) + 5;
  const X = (f) => mL + ((f - r.start_hz) / (r.stop_hz - r.start_hz || 1)) * (cv.width - mL - 12);
  const Y = (v) => mT + (1 - (v - lo) / (hi - lo)) * (cv.height - mT - mB);
  ctx.strokeStyle = "#1b2331"; ctx.fillStyle = "#7d8ba0"; ctx.font = "11px monospace";
  for (let g = 0; g <= 6; g++) {
    const gx = mL + (g / 6) * (cv.width - mL - 12);
    ctx.beginPath(); ctx.moveTo(gx, mT); ctx.lineTo(gx, cv.height - mB); ctx.stroke();
    ctx.fillText(fmtHz(r.start_hz + (g / 6) * (r.stop_hz - r.start_hz)), gx - 26, cv.height - 8);
  }
  for (let g = 0; g <= 4; g++) {
    const v = lo + (g / 4) * (hi - lo);
    ctx.fillText(v.toFixed(0), 6, Y(v) + 4);
  }
  // peak bars, then the noise-floor trace on top
  pts.forEach((p) => {
    const busy = p.occupancy > 0.02;
    ctx.fillStyle = busy ? "#e0a52e" : "#2b6f5e";
    const w = Math.max(3, (cv.width - mL - 12) / pts.length - 2);
    ctx.fillRect(X(p.center_hz) - w / 2, Y(p.peak_dbfs), w,
                 Y(p.noise_floor_dbfs) - Y(p.peak_dbfs));
  });
  ctx.strokeStyle = "#35c4a2"; ctx.lineWidth = 1.5; ctx.beginPath();
  pts.forEach((p, i) => i ? ctx.lineTo(X(p.center_hz), Y(p.noise_floor_dbfs))
                          : ctx.moveTo(X(p.center_hz), Y(p.noise_floor_dbfs)));
  ctx.stroke();
  ctx.fillStyle = "#7d8ba0";
  ctx.fillText("bars = peak above floor (amber = occupied) · line = noise floor",
               mL + 8, mT + 12);
}

/* ---------- World View (release 0.4) ---------- */
let SCENE = null;
let selectedFinding = null;

async function refreshSites() {
  const sites = await api("/api/sites");
  const sel = $("site-select");
  const cur = sel.value;
  sel.innerHTML = sites.map((s) =>
    `<option value="${esc(s.site_id)}">${esc(s.name)} (${s.num_scans} scans)</option>`).join("");
  if (sites.some((s) => s.site_id === cur)) sel.value = cur;
  const scans = await api("/api/experiments?kind=scan");
  $("site-scan").innerHTML = scans.map((e) =>
    `<option value="${esc(e.experiment_id)}">${esc(e.name)} — ${esc(e.experiment_id)}</option>`).join("")
    || '<option value="">no finalized scans</option>';
  if (sites.length) buildScene();
}

$("site-add").onclick = async () => {
  const name = $("site-name").value.trim();
  if (!name) { alert("name the site"); return; }
  const s = await api("/api/sites", { method: "POST", body: { name } });
  $("site-name").value = "";
  await refreshSites();
  $("site-select").value = s.site_id;
};

$("reg-add").onclick = async () => {
  const sid = $("site-select").value, exp = $("site-scan").value;
  if (!sid || !exp) { alert("pick a site and a finalized scan"); return; }
  try {
    await api(`/api/sites/${sid}/register`, { method: "POST", body: {
      experiment_id: exp,
      origin_x_m: parseFloat($("reg-x").value) || 0,
      origin_y_m: parseFloat($("reg-y").value) || 0,
      heading_deg: parseFloat($("reg-h").value) || 0,
      label: $("reg-label").value.trim(),
    }});
    $("reg-label").value = "";
    await refreshSites();
  } catch (e) { $("site-status").textContent = e.message; }
};

$("site-refresh").onclick = buildScene;
$("slice-on").onchange = buildScene;

async function buildScene() {
  const sid = $("site-select").value;
  if (!sid) return;
  $("site-status").textContent = "building scene…";
  let url = `/api/sites/${sid}/scene?tolerance_m=${$("site-tol").value}`;
  if ($("slice-on").checked) url += `&slice_depth_m=${$("slice-depth").value}`;
  try {
    SCENE = await api(url);
    const confirmed = SCENE.findings.filter((f) => f.supporting_scans >= 2).length;
    $("site-status").textContent =
      `${SCENE.scans.length} scan(s) · ${SCENE.findings.length} finding(s), ` +
      `${confirmed} confirmed by 2+ scans` +
      (SCENE.errors.length ? ` · ${SCENE.errors.length} scan(s) skipped` : "");
    $("world-meta").textContent = " — " + SCENE.site.coordinate_system;
    const ms = $("mig-select");
    ms.innerHTML = SCENE.scans.map((s) =>
      `<option value="${esc(s.experiment_id)}">${esc(s.placement.label)}</option>`).join("");
    drawWorld();
    renderFindings();
    drawMigrated();
    if (SCENE.errors.length) {
      $("finding-detail").textContent =
        "Skipped scans:\n" + SCENE.errors.map((e) => ` ${e.experiment_id}: ${e.error}`).join("\n");
    }
  } catch (e) { $("site-status").textContent = "failed: " + e.message; }
}
$("mig-select").onchange = drawMigrated;
$("mig-gain").onchange = drawMigrated;

function worldBounds() {
  const xs = [], ys = [];
  SCENE.scans.forEach((s) => s.path.forEach((p) => { xs.push(p[0]); ys.push(p[1]); }));
  SCENE.findings.forEach((f) => { xs.push(f.site_x_m); ys.push(f.site_y_m); });
  if (!xs.length) { xs.push(0, 1); ys.push(0, 1); }
  const pad = 0.6;
  return { x0: Math.min(...xs) - pad, x1: Math.max(...xs) + pad,
           y0: Math.min(...ys) - pad, y1: Math.max(...ys) + pad };
}

function drawWorld() {
  const cv = $("world-map"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  if (!SCENE) return;
  const b = worldBounds();
  const m = 40;
  const span = Math.max(b.x1 - b.x0, b.y1 - b.y0);   // keep aspect square
  const X = (x) => m + ((x - b.x0) / span) * (cv.width - 2 * m);
  const Y = (y) => cv.height - m - ((y - b.y0) / span) * (cv.height - 2 * m);

  // grid + axes
  ctx.strokeStyle = "#161d29"; ctx.lineWidth = 1;
  for (let g = 0; g <= 8; g++) {
    const gx = m + (g / 8) * (cv.width - 2 * m);
    const gy = m + (g / 8) * (cv.height - 2 * m);
    ctx.beginPath(); ctx.moveTo(gx, m); ctx.lineTo(gx, cv.height - m); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(m, gy); ctx.lineTo(cv.width - m, gy); ctx.stroke();
  }
  ctx.fillStyle = "#7d8ba0"; ctx.font = "11px monospace";
  ctx.fillText(`${b.x0.toFixed(1)} m`, m - 12, cv.height - m + 16);
  ctx.fillText(`${(b.x0 + span).toFixed(1)} m`, cv.width - m - 24, cv.height - m + 16);
  ctx.fillText(`${b.y0.toFixed(1)} m`, 4, cv.height - m + 4);
  ctx.fillText(`${(b.y0 + span).toFixed(1)} m`, 4, m + 4);

  // depth-slice samples (measured paths only)
  if (SCENE.depth_slice) {
    const s = SCENE.depth_slice.samples;
    const vals = s.map((p) => p.amplitude_db);
    const lo = Math.min(...vals), hi = Math.max(...vals);
    s.forEach((p) => {
      const [r, g, bl] = viridis((p.amplitude_db - lo) / (hi - lo + 1e-9));
      ctx.fillStyle = `rgb(${r},${g},${bl})`;
      ctx.beginPath(); ctx.arc(X(p.x_m), Y(p.y_m), 4, 0, 7); ctx.fill();
    });
  }

  // scan paths — direct measurement, drawn solid (UX-WLD-005)
  SCENE.scans.forEach((s, i) => {
    if (s.path.length < 2) return;
    ctx.strokeStyle = "#4aa3ff"; ctx.lineWidth = 2.5;
    ctx.beginPath();
    ctx.moveTo(X(s.path[0][0]), Y(s.path[0][1]));
    ctx.lineTo(X(s.path[1][0]), Y(s.path[1][1]));
    ctx.stroke();
    ctx.fillStyle = "#8db8e8"; ctx.font = "11px monospace";
    ctx.fillText(s.placement.label, X(s.path[1][0]) + 6, Y(s.path[1][1]) - 4);
  });

  // findings — algorithmic inference, drawn as rings (UX-WLD-005)
  SCENE.findings.forEach((f, i) => {
    const confirmed = f.supporting_scans >= 2;
    const sel = selectedFinding === i;
    ctx.strokeStyle = confirmed ? "#35c4a2" : "#e0a52e";
    ctx.fillStyle = confirmed ? "rgba(53,196,162,0.35)" : "rgba(224,165,46,0.12)";
    ctx.lineWidth = sel ? 3.5 : 2;
    ctx.beginPath();
    ctx.arc(X(f.site_x_m), Y(f.site_y_m), 11, 0, 7);
    ctx.fill(); ctx.stroke();
    // positional spread across supporting scans = visible uncertainty
    if (f.position_spread_m > 0) {
      const rpx = Math.abs(X(b.x0 + f.position_spread_m) - X(b.x0));
      ctx.setLineDash([3, 3]); ctx.lineWidth = 1;
      ctx.beginPath(); ctx.arc(X(f.site_x_m), Y(f.site_y_m), Math.max(rpx, 12), 0, 7);
      ctx.stroke(); ctx.setLineDash([]);
    }
    ctx.fillStyle = "#d7e0ea"; ctx.font = "bold 11px monospace";
    ctx.fillText(String(i + 1), X(f.site_x_m) - 3, Y(f.site_y_m) + 4);
  });
}

$("world-map").onclick = (ev) => {
  if (!SCENE || !SCENE.findings.length) return;
  const cv = $("world-map"), rect = cv.getBoundingClientRect();
  const px = (ev.clientX - rect.left) * (cv.width / rect.width);
  const py = (ev.clientY - rect.top) * (cv.height / rect.height);
  const b = worldBounds(), m = 40;
  const span = Math.max(b.x1 - b.x0, b.y1 - b.y0);
  const X = (x) => m + ((x - b.x0) / span) * (cv.width - 2 * m);
  const Y = (y) => cv.height - m - ((y - b.y0) / span) * (cv.height - 2 * m);
  let best = null, bd = 20;
  SCENE.findings.forEach((f, i) => {
    const d = Math.hypot(X(f.site_x_m) - px, Y(f.site_y_m) - py);
    if (d < bd) { bd = d; best = i; }
  });
  if (best !== null) selectFinding(best);
};

function renderFindings() {
  $("findings-list").innerHTML = SCENE.findings.map((f, i) => `
    <div class="expitem ${selectedFinding === i ? "sel" : ""}"
         onclick="selectFinding(${i})">
      <div><b>#${i + 1}</b> (${f.site_x_m.toFixed(2)}, ${f.site_y_m.toFixed(2)}) m
        · depth ${f.depth_m.toFixed(2)} m
        <span class="mut">[${f.depth_interval_m[0].toFixed(2)}–${f.depth_interval_m[1].toFixed(2)}]</span><br>
        <span class="mut">${esc(f.classification)}</span>
        ${findingActions(i)}</div>
      <div><span class="tag" style="${f.supporting_scans >= 2
        ? "background:#12241d;color:#86dfc0" : ""}">${f.supporting_scans} scan${f.supporting_scans > 1 ? "s" : ""}</span>
        <span class="tag">${esc(f.confidence.overall)}</span></div>
    </div>`).join("") || '<span class="mut">no findings — register scans and build the scene</span>';
}

window.explainFinding = async (i) => {
  const sid = $("site-select").value;
  document.querySelector('nav button[data-tab="sage"]').click();
  await refreshSage();
  $("sage-site").value = sid;
  $("sage-q").value = `Why is finding ${i + 1} highlighted?`;
  const r = await api(`/api/sage/site/${sid}/finding/${i}`);
  renderAnswer(r); narrateLater(r);
};

window.selectFinding = (i) => {
  selectedFinding = i;
  const f = SCENE.findings[i];
  $("finding-detail").textContent = JSON.stringify({
    position: { x_m: f.site_x_m, y_m: f.site_y_m,
                spread_m: f.position_spread_m },
    depth_m: f.depth_m, depth_interval_m: f.depth_interval_m,
    supporting_scans: f.supporting_scans,
    confidence: f.confidence,
    epistemic: f.epistemic,
    evidence: f.evidence,
  }, null, 1);
  renderFindings();
  drawWorld();
};

// "why is this highlighted?" belongs next to the thing being asked about
function findingActions(i) {
  return `<button style="margin-top:6px"
      onclick="event.stopPropagation(); explainFinding(${i})">Ask SAGE why ↗</button>`;
}

function drawMigrated() {
  const cv = $("mig-plot"), ctx = cv.getContext("2d");
  ctx.fillStyle = "#0a0d11"; ctx.fillRect(0, 0, cv.width, cv.height);
  if (!SCENE) return;
  const mig = SCENE.migrated[$("mig-select").value];
  if (!mig) return;
  const cols = mig.positions_m.length, rows = mig.depths_m.length;
  const mL = 52, mB = 26;
  const cw = (cv.width - mL) / cols, ch = (cv.height - mB) / rows;

  // Migrated amplitude falls off steeply with depth, so a fixed colour scale
  // shows a bright shallow wash and nothing else. Depth gain re-expresses each
  // cell as its contrast against the mean response at the same depth, which is
  // what makes a focused target visible. Display only — the stored data and
  // every reported number are unaffected.
  const gain = $("mig-gain").checked;
  const rowMean = [];
  for (let j = 0; j < rows; j++) {
    let s = 0;
    for (let i = 0; i < cols; i++) s += mig.amplitude_db[i][j];
    rowMean.push(s / cols);
  }
  for (let i = 0; i < cols; i++) {
    for (let j = 0; j < rows; j++) {
      const raw = mig.amplitude_db[i][j];
      const v = gain ? (raw - rowMean[j]) / 12 : (raw + 18) / 18;
      const [r, g, b] = viridis(v);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(mL + i * cw, j * ch, Math.ceil(cw), Math.ceil(ch));
    }
  }
  ctx.fillStyle = "#7d8ba0"; ctx.font = "11px monospace";
  for (let g = 0; g <= 5; g++) {
    const d = mig.depths_m[Math.floor((rows - 1) * g / 5)];
    ctx.fillText(d.toFixed(1) + " m", 4, (g / 5) * (cv.height - mB - 8) + 12);
  }
  for (let g = 0; g <= 6; g++) {
    const p = mig.positions_m[Math.floor((cols - 1) * g / 6)];
    ctx.fillText(p.toFixed(1) + " m", mL + (g / 6) * (cv.width - mL - 40), cv.height - 6);
  }
  $("mig-meta").innerHTML =
    ` — diffraction-stack, aperture ${mig.aperture_m.toFixed(1)} m, ` +
    `${mig.measured_columns} measured columns, ` +
    `supported to ${mig.max_supported_depth_m} m` +
    (mig.depth_focus_warning
      ? `<br><span style="color:#e8c96a">⚠ ${esc(mig.depth_focus_warning)}</span>`
      : "");
}

$("site-report").onclick = async () => {
  const sid = $("site-select").value;
  if (!sid) return;
  try {
    const r = await api(`/api/sites/${sid}/report`);
    const blob = new Blob([r.markdown], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `forge-vision-site-${sid}.md`;
    a.click();
    $("finding-detail").textContent = r.markdown;
    $("site-status").textContent = "report exported";
  } catch (e) { $("site-status").textContent = "report failed: " + e.message; }
};

/* ---------- SAGE assistant (release 0.5) ---------- */
const KIND_STYLE = {
  observation: "background:#12241d;color:#86dfc0",
  calculation: "background:#101f2e;color:#8db8e8",
  inference:   "background:#2b2410;color:#e8c96a",
  hypothesis:  "background:#2a1a2e;color:#d8a0e0",
  unknown:     "background:#1c1c22;color:#9aa4b2",
};
const SUGGESTED = [
  "Why is finding 1 highlighted?",
  "Show anomalies between 0.5 and 2 meters deep",
  "Which findings are confirmed by more than one scan?",
  "What should I measure next?",
  "What is wrong with this experiment?",
  "Summarize this experiment",
];

async function refreshSage() {
  const sites = await api("/api/sites");
  const ss = $("sage-site");
  const cur = ss.value;
  ss.innerHTML = '<option value="">— no site —</option>' + sites.map((s) =>
    `<option value="${esc(s.site_id)}">${esc(s.name)}</option>`).join("");
  if (sites.some((s) => s.site_id === cur)) ss.value = cur;
  else if (sites.length) ss.value = sites[0].site_id;

  const exps = await api("/api/experiments");
  const es = $("sage-exp");
  const ecur = es.value;
  es.innerHTML = '<option value="">— no experiment —</option>' + exps.slice(0, 40)
    .map((e) => `<option value="${esc(e.experiment_id)}">${esc(e.name)} (${esc(e.kind)})</option>`)
    .join("");
  if (exps.some((e) => e.experiment_id === ecur)) es.value = ecur;

  refreshLlm();
  $("sage-suggest").innerHTML = SUGGESTED.map((q) =>
    `<button style="margin:0 6px 6px 0" onclick="sageAsk(${JSON.stringify(q).replace(/"/g, "&quot;")})">${esc(q)}</button>`).join("");
}

window.sageAsk = async (q) => {
  $("sage-q").value = q;
  await doAsk(q);
};
$("sage-ask").onclick = () => doAsk($("sage-q").value);
$("sage-q").addEventListener("keydown", (e) => {
  if (e.key === "Enter") doAsk($("sage-q").value);
});

async function doAsk(question) {
  $("sage-status").textContent = "reasoning over stored evidence…";
  try {
    const r = await api("/api/sage/ask", { method: "POST", body: {
      question, site_id: $("sage-site").value,
      experiment_id: $("sage-exp").value } });
    renderAnswer(r);
    narrateLater(r);
    $("sage-status").textContent = "";
  } catch (e) { $("sage-status").textContent = "failed: " + e.message; }
}

$("sage-quality").onclick = async () => {
  const id = $("sage-exp").value;
  if (!id) { alert("select an experiment"); return; }
  const r = await api(`/api/sage/experiment/${id}`);
  renderAnswer(r); narrateLater(r);
};
$("sage-next").onclick = async () => {
  const id = $("sage-site").value;
  if (!id) { alert("select a site"); return; }
  const r = await api(`/api/sage/site/${id}/recommend`);
  renderAnswer(r); narrateLater(r);
};

/* --- LLM endpoint configuration --- */
async function refreshLlm() {
  const r = await api("/api/llm");
  const h = r.health[r.active];
  $("llm-list").innerHTML = r.endpoints.map((e) => `
    <div class="expitem">
      <div><b>${esc(e.name)}</b> ${e.enabled
        ? '<span class="tag" style="background:#12241d;color:#86dfc0">active</span>' : ""}
        <br><span class="mut">${esc(e.base_url)} · ${esc(e.model || "no model")}
        · ${e.max_tokens} max tokens · ${e.timeout_s}s timeout</span></div>
      <div><button onclick="llmProbeNamed('${esc(e.name)}')">Health</button>
        <button onclick="llmDelete('${esc(e.name)}')">Delete</button></div>
    </div>`).join("") || '<span class="mut">no endpoints configured — narration off</span>';
  if (h) {
    $("llm-status").textContent = h.reachable
      ? `active endpoint reachable in ${h.latency_s}s · models: ${h.models.join(", ")}`
      : `active endpoint unreachable: ${h.error}`;
  }
}

$("llm-probe").onclick = async () => {
  const url = $("llm-url").value.trim();
  if (!url) { alert("enter a base URL ending in /v1"); return; }
  $("llm-status").textContent = "probing…";
  try {
    await api("/api/llm", { method: "POST", body: {
      name: $("llm-name").value.trim() || "probe", base_url: url,
      api_key: $("llm-key").value, enabled: false } });
    const h = await api(`/api/llm/${$("llm-name").value.trim() || "probe"}/health`);
    $("llm-model").innerHTML = h.models.map((m) => `<option>${esc(m)}</option>`).join("")
      || '<option value="">no models</option>';
    $("llm-status").textContent = h.reachable
      ? `reachable in ${h.latency_s}s · ${h.models.length} model(s)`
      : `unreachable: ${h.error}`;
    refreshLlm();
  } catch (e) { $("llm-status").textContent = "probe failed: " + e.message; }
};

$("llm-save").onclick = async () => {
  try {
    await api("/api/llm", { method: "POST", body: {
      name: $("llm-name").value.trim() || "local",
      base_url: $("llm-url").value.trim(),
      model: $("llm-model").value,
      api_key: $("llm-key").value,
      max_tokens: parseInt($("llm-maxtok").value, 10) || 700,
      enabled: $("llm-enabled").checked } });
    $("llm-status").textContent = "saved";
    refreshLlm();
  } catch (e) { $("llm-status").textContent = "save failed: " + e.message; }
};

window.llmProbeNamed = async (name) => {
  const h = await api(`/api/llm/${name}/health`);
  $("llm-status").textContent = h.reachable
    ? `${name}: reachable in ${h.latency_s}s · models: ${h.models.join(", ")}`
    : `${name}: unreachable — ${h.error}`;
};
window.llmDelete = async (name) => {
  await api(`/api/llm/${name}/delete`, { method: "POST" });
  refreshLlm();
};

function renderNarration(n) {
  if (!n) return "";
  if (!n.available) {
    return `<div class="alert warn"><b>Narration unavailable.</b>
      ${esc(n.error || "")} ${esc(n.note || "")}</div>`;
  }
  if (!n.grounded) {
    return `<div class="alert err"><b>Narration withheld.</b> ${esc(n.note)}
      <details style="margin-top:6px"><summary class="mut">show what the model
      wrote (not an instrument output)</summary>
      <div style="margin-top:6px;opacity:.75">${esc(n.withheld_text)}</div>
      </details></div>`;
  }
  return `<div class="panel" style="background:#101820;border-color:#1f4a38">
      <div style="margin-bottom:6px">
        <span class="tag" style="background:#12241d;color:#86dfc0">narration</span>
        <span class="mut">${esc(n.model)} via ${esc(n.endpoint)} ·
        ${n.latency_s}s · every figure checked against the findings below</span>
      </div>
      <div>${esc(n.text)}</div></div>`;
}

// Findings render immediately; narration arrives later. A local model can
// take a minute or more, and the instrument must not appear to hang on it.
async function narrateLater(r) {
  if (!r.narration_available || !r.facts || !r.facts.length) return;
  const slot = $("sage-narration");
  if (!slot) return;
  slot.innerHTML = '<div class="alert warn">Narrating with the local model… '
    + 'the findings below are already complete and will not change.</div>';
  try {
    const n = await api("/api/sage/narrate", { method: "POST", body: r });
    slot.innerHTML = renderNarration(n);
  } catch (e) {
    slot.innerHTML = `<div class="alert warn">Narration failed: ${esc(e.message)}
      — the findings below are unaffected.</div>`;
  }
}

function renderAnswer(r) {
  const sev = { critical: "#e0492e", warn: "#e0a52e", info: "#263042" };
  if (!r.understood) {
    $("sage-answer").innerHTML =
      `<div class="alert warn"><b>Not answered.</b> ${esc(r.note)}</div>`;
    $("sage-meta").textContent = "";
    return;
  }
  $("sage-meta").textContent =
    ` — ${r.facts.length} statement(s), ${r.evidence_count} evidence link(s)` +
    (r.question ? ` · “${r.question}”` : "");
  const note = r.note ? `<div class="alert warn">${esc(r.note)}</div>` : "";
  $("sage-answer").innerHTML = note + '<div id="sage-narration"></div>'
    + renderNarration(r.narration)
    + (r.facts.map((f) => `
    <div class="panel" style="border-left:3px solid ${sev[f.severity] || sev.info};
         background:var(--panel2); margin-bottom:9px">
      <div style="margin-bottom:6px">
        <span class="tag" style="${KIND_STYLE[f.kind] || ""}"
              title="${esc(f.kind_meaning)}">${esc(f.kind)}</span>
        ${f.severity !== "info"
          ? `<span class="tag" style="background:#2d1410;color:#f0a08e">${esc(f.severity)}</span>` : ""}
        <span class="mut">${esc(f.kind_meaning)}</span>
      </div>
      <div>${esc(f.statement)}</div>
      ${f.action ? `<div class="mut" style="margin-top:6px">→ ${esc(f.action)}</div>` : ""}
      ${f.evidence.length ? `<div style="margin-top:7px">${f.evidence.map((e) =>
        e.type === "experiment"
          ? `<span class="tag" style="cursor:pointer"
                   onclick="openLibrary('${esc(e.experiment_id)}')"
                   title="${esc(e.detail || "")}">${esc(e.artifact || "experiment")}
             ${esc(e.locator || "")} ↗</span>`
          : `<span class="tag">site ${esc(e.site_id)}</span>`).join(" ")}</div>` : ""}
    </div>`).join("") ||
    '<p class="mut">No statements — nothing in the stored data matched.</p>');
}

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
