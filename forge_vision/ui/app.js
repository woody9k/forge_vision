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
    if (b.dataset.tab === "hardware") refreshComponents();
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

const LINK_LABEL = { network: "Ethernet", usb: "USB",
                     "usb-gadget": "USB (network gadget)",
                     simulated: "Simulated" };

function linkLine(d) {
  const L = d.link || { kind: "simulated" };
  const speed = L.throughput_mb_s ? `${L.throughput_mb_s} MB/s` : "";
  return `<span class="dot ${d.connected ? "on" : "off"}"></span>
    <b>${esc(d.kind === "simulated_pluto_plus" ? "Simulated radio" : "Pluto")}</b>
    <span class="linkbadge link-${esc(L.kind)}">${esc(LINK_LABEL[L.kind] || L.kind)}</span>
    ${L.address ? `<span class="addr">${esc(L.address)}</span>` : ""}
    ${speed ? `<span class="mut"> · ${esc(speed)}</span>` : ""}
    ${d.tx_enabled ? '<span class="tag" style="background:#57120a;color:#ffb4a4">TX ON</span>' : ""}`;
}

function settingsLine(d) {
  return `${fmtHz(d.config.center_frequency_hz)} ·
    ${(d.config.sample_rate_hz / 1e6).toFixed(2)} MSPS ·
    RX ${d.config.rx_gain_db} dB · TX ${d.config.tx_gain_db} dB`;
}

// Dashboard: what is in force, with no way to change it from here.
function renderDashRadios(devices) {
  if (!$("dash-radio")) return;
  const live = devices.filter((d) => d.connected);
  const shown = live.length ? live : devices;
  // The settings line is what the radio was asked for. Without the sync line
  // beside it this panel will happily read "915 MHz · RX 40 dB" while the
  // radio sits at 923 MHz with AGC driving the gain to 73 — which is the
  // exact failure this is meant to end, on the page most likely to be glanced
  // at. No resync button here: the Dashboard reports, Hardware acts.
  $("dash-radio").innerHTML = shown.map((d) => `
    <div class="devcard">
      <div class="devmain">${linkLine(d)}<br>
        <span class="mut">${settingsLine(d)}</span>
        ${syncLine(d, false)}
        ${d.connected ? "" : '<br><span class="mut">not connected</span>'}
      </div>
    </div>`).join("") || '<span class="mut">no radios</span>';
}

// Hardware: the same radios, with the controls that change them.
// The settings line above shows what the radio was *asked* for. This says
// whether it still holds them. "Not checked" is rendered as its own state
// rather than collapsing into "fine", because a stale reading and a confirmed
// one are different claims (rule 3).
function syncLine(d, allowActions = true) {
  if (!d.connected) return "";
  const s = d.sync;
  if (!s) {
    return `<br><span class="tag" style="background:#1a1a20;border:1px solid #333;
      color:#888">radio state not yet checked</span>`;
  }
  if (s.readable === false) {
    return `<br><span class="tag" style="background:#2b2410;border:1px solid #5c4c1c;
      color:#e8c96a">cannot read radio state${
        s.error ? " — " + esc(s.error) : ""}</span>`;
  }
  const age = s.checked_at
    ? Math.max(0, Math.round(Date.now() / 1000 - s.checked_at)) : null;
  if (s.in_sync) {
    return `<br><span class="tag" style="background:#12241d;border:1px solid #1f4a38;
      color:#86dfc0">radio matches these settings${
        age !== null ? ` · checked ${age}s ago` : ""}</span>`;
  }
  const rows = (s.drift || []).map((x) =>
    `<div><code>${esc(x.field)}</code>: asked for <b>${esc(String(x.requested))}</b>,
     radio has <b>${esc(String(x.actual))}</b></div>`).join("");
  return `<br><span class="tag" style="background:#1c1116;border:1px solid #3a2028;
      color:#a06070;display:block;padding:6px 8px">
      <b>The radio is not holding these settings.</b>${
        age !== null ? ` Checked ${age}s ago.` : ""}
      ${rows}
      ${allowActions
        ? `<button onclick="resyncDevice('${esc(d.device_id)}')">Adopt the
             radio's values</button>`
        : `<span class="mut">Resolve this under <b>Hardware</b>.</span>`}
      <span class="mut">Captures record what the radio actually had, so this
        does not corrupt data — but the controls above are describing something
        the radio is not doing.</span>
    </span>`;
}

window.resyncDevice = async (id) => {
  try {
    const r = await api(`/api/devices/${id}/resync`, { method: "POST" });
    if (r.note) alert(r.note);
    if (r.tx_revoked) alert("Transmit permission was withdrawn: the approved "
      + "configuration no longer describes this radio.");
  } catch (e) {
    alert("Resync failed: " + String(e.message || e));
  }
  refreshStatus();
};

function renderDeviceCards(devices) {
  if (!$("device-list")) return;
  $("device-list").innerHTML = devices.map((d) => {
    const L = d.link || { kind: "simulated" };
    // One button per genuinely different kind of link, fastest of each, and
    // never the kind we are already using.
    const byKind = new Map();
    for (const a of (L.alternatives || [])) {
      if (a.error || a.kind === L.kind) continue;
      const cur = byKind.get(a.kind);
      if (!cur || (a.throughput_mb_s || 0) > (cur.throughput_mb_s || 0)) byKind.set(a.kind, a);
    }
    const alts = [...byKind.values()];
    return `
    <div class="devcard">
      <div class="devmain">${linkLine(d)}<br>
        <span class="mut">${settingsLine(d)}</span>
        ${syncLine(d)}
        <details>
          <summary>capabilities &amp; notes</summary>
          <span class="mut">
            id <code>${esc(d.device_id)}</code><br>
            tuning ${fmtHz(d.capabilities.min_frequency)}–${fmtHz(d.capabilities.max_frequency)} ·
            max BW ${(d.capabilities.max_bandwidth / 1e6).toFixed(0)} MHz<br>
            waveforms: ${(d.compatible_waveforms || []).map(esc).join(", ") || "none"}
            ${L.chosen_because ? "<br>chose this link: " + esc(L.chosen_because) : ""}
            ${(d.capability_notes || []).map((n) => "<br>" + esc(n)).join("")}
          </span>
        </details>
      </div>
      <div class="devactions">
        ${d.connected
          ? `<button onclick="devDisconnect('${esc(d.device_id)}')">Disconnect</button>`
          : `<button onclick="devConnect('${esc(d.device_id)}')">Connect</button>`}
        ${alts.map((a) => `<button title="${esc(a.uri)}"
             onclick="switchTransport('${esc(d.device_id)}','${esc(a.uri)}')"
             >Use ${esc(LINK_LABEL[a.kind] || a.kind)}${
               a.throughput_mb_s ? ` (${a.throughput_mb_s} MB/s)` : ""}</button>`).join("")}
        ${L.kind === "simulated" ? ""
          : `<button onclick="forgetDevice('${esc(d.device_id)}')">Forget</button>`}
      </div>
    </div>`; }).join("");
}

async function refreshStatus() {
  try {
    STATUS = await api("/api/status");
  } catch (e) { return; }
  setTxIndicator(STATUS.safety);
  renderDashboard();
  fillSelectors();
  refreshJobs();
  refreshChain();
  refreshRxProtection();
  refreshPositionUi();
  syncServerMirroredFields();
}

// Fields whose value belongs to the server rather than to this page. They
// were write-only: read on submit, never populated, so they showed their HTML
// default no matter what the platform actually held. `atten-db` is the one
// that matters — it feeds the receive-protection estimate, and a form stuck
// at 0 invites re-submitting 0 over a real declared value.
const mirroredLastSynced = {};

function syncServerMirroredFields() {
  const set = (id, value) => {
    const el = $(id);
    if (!el || value === undefined || value === null) return;
    // Never fight a keystroke, and never fight an edit that has lost focus
    // but not yet been submitted. `activeElement` alone is not enough: focus
    // moves to the Declare button on mousedown, so a poll landing between
    // mousedown and click would revert the field and submit the old value —
    // and path attenuation is a safety declaration, not a preference.
    const dirty = mirroredLastSynced[id] !== undefined
      && String(el.value) !== String(mirroredLastSynced[id]);
    if (document.activeElement === el || dirty) return;
    el.value = value;
    mirroredLastSynced[id] = String(value);
  };
  if (STATUS && STATUS.safety) set("atten-db", STATUS.safety.path_attenuation_db);
  if (!configFormLoaded && $("live-device") && $("live-device").value) {
    configFormLoaded = true;
    syncDeviceConfigInputs($("live-device").value);
  } else {
    renderConfigDrift();
  }
}
let configFormLoaded = false;

// The frequency profile persists across restarts. When it could *not* be
// restored the platform falls back to the built-in default, and that has to
// look different from a profile the operator chose — silently widening from
// an ISM profile back to 70 MHz-6 GHz is the failure this persistence exists
// to end, and it would be invisible if the row just showed a name.
function profileSourceNote(sf) {
  const src = sf.profile_source;
  if (!src || !src.note) return "";
  return ` <span class="tag" style="background:#2b2410;border:1px solid #5c4c1c;
    color:#e8c96a" title="${esc(src.note)}">default — not restored</span>`;
}

function renderDashboard() {
  const s = STATUS;
  renderDeviceCards(s.devices);
  renderDashRadios(s.devices);
  renderAttention(s);

  const st = s.storage, sf = s.safety;
  $("system-info").innerHTML = `
    <div class="sysrow"><span class="k">Version</span><span>${esc(s.version)}</span></div>
    <div class="sysrow"><span class="k">Captures stored</span>
      <span>${fmtBytes(st.experiments_bytes)}</span></div>
    <div class="sysrow"><span class="k">Disk free</span>
      <span>${fmtBytes(st.disk_free_bytes)}${
        st.low_space_warning ? ' <span style="color:var(--warn)">LOW</span>' : ""}</span></div>
    <div class="sysrow"><span class="k">Transmit</span>
      <span>${sf.armed
        ? `<span style="color:var(--warn)">armed by ${esc(sf.armed_by)}</span>`
        : "disarmed"}</span></div>
    <div class="sysrow"><span class="k">Safety profile</span>
      <span>${esc(sf.limits.active_profile)}${profileSourceNote(sf)}</span></div>
    <div class="sysrow"><span class="k">Scans in progress</span>
      <span>${Object.keys(s.active_scans).length}</span></div>`;
  refreshRadioBook();
  $("recent-experiments").innerHTML = s.recent_experiments.map((e) => `
    <div class="expitem" onclick="openLibrary('${e.experiment_id}')">
      <div><b>${esc(e.name)}</b> <span class="tag">${esc(e.kind)}</span>
        <span class="mut">${e.num_segments} seg · ${e.derived.map(esc).join(", ")}</span></div>
      <div class="mut">${new Date(e.started_at * 1000).toLocaleString()} · ${esc(e.status)}</div>
    </div>`).join("") || '<span class="mut">none yet</span>';
}

// Which radio a fresh page should be pointed at. `sim-pluto-0` is registered
// first, so with no selection to preserve the browser picked the *simulator*
// on every page load — including with a real radio connected. An operator
// would then read the config panel, adjust a gain and press Apply, having
// configured the simulator while the radio they were working with sat
// untouched, and the Dashboard (which shows the real one) disagreed with the
// Live RF form for reasons nothing on screen explained.
function preferredDevice(devices) {
  const real = devices.filter((d) => d.kind !== "simulated_pluto_plus");
  return (real.find((d) => d.connected) || real[0]
          || devices.find((d) => d.connected) || devices[0] || {}).device_id;
}

function fillSelectors() {
  const devs = STATUS.devices.map((d) => d.device_id);
  const preferred = preferredDevice(STATUS.devices);
  for (const id of ["live-device", "range-device", "scan-device"]) {
    const sel = $(id);
    const cur = sel.value;
    sel.innerHTML = devs.map((d) => `<option>${esc(d)}</option>`).join("");
    // Preserve a deliberate choice; otherwise prefer a real radio over the
    // simulator rather than whichever happens to be registered first.
    sel.value = devs.includes(cur) ? cur : (preferred || devs[0]);
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

window.forgetDevice = async (id) => {
  if (!confirm(`Forget ${id}?\n\nThis removes it from the list. The next scan will find it again.`)) return;
  try { await api(`/api/devices/${encodeURIComponent(id)}/forget`, { method: "POST" }); }
  catch (e) { alert(e.message || e); }
  refreshStatus();
};

window.switchTransport = async (id, uri) => {
  $("rescan-status").textContent = `switching to ${uri}…`;
  try {
    await api(`/api/devices/${encodeURIComponent(id)}/switch_transport`,
              { method: "POST", body: { uri } });
    $("rescan-status").textContent = `now on ${uri}`;
  } catch (e) { $("rescan-status").textContent = `could not switch: ${e.message || e}`; }
  refreshStatus();
};

async function refreshRadioBook() {
  if (!$("radio-book")) return;
  let list;
  try { list = await api("/api/radios"); } catch (e) { return; }
  $("radio-book").innerHTML = list.map((r) => `
    <div class="pathrow">
      <span class="grow">${esc(r.label)}
        <span class="addr">${esc(r.uri)}</span>
        ${r.in_use ? '<span class="tag">in use</span>' : ""}
        ${r.overridden_by_env
          ? '<span class="tag" style="background:#3a2f12;color:#e8c96a">overridden by FORGE_VISION_PLUTO_URIS</span>'
          : ""}</span>
      <button onclick="removeRadio('${esc(r.radio_id)}')">Remove</button>
    </div>`).join("") ||
    '<div class="mut" style="padding:4px 0">none saved — USB and pluto.local are always checked</div>';
}

window.removeRadio = async (id) => {
  await api(`/api/radios/${encodeURIComponent(id)}/delete`, { method: "POST" });
  refreshRadioBook();
};

window.devConnect = async (id) => { await api(`/api/devices/${id}/connect`, { method: "POST" }); refreshStatus(); };
window.devDisconnect = async (id) => { await api(`/api/devices/${id}/disconnect`, { method: "POST" }); refreshStatus(); };

$("estop").onclick = async () => {
  const r = await api("/api/safety/stop", { method: "POST" });
  alert("EMERGENCY STOP\n" + r.results.join("\n"));
  refreshStatus();
};

/* ---------- processing queue, RF chain, RX protection ---------- */
async function refreshJobs() {
  let r;
  try { r = await api("/api/jobs"); } catch (e) { return; }
  $("job-list").innerHTML = r.jobs.slice(0, 8).map((j) => {
    const pct = Math.round(j.progress * 100);
    const colour = { succeeded: "#35c4a2", failed: "#e0492e",
                     cancelled: "#7d8ba0", running: "#4aa3ff",
                     queued: "#7d8ba0" }[j.state];
    return `<div class="expitem">
      <div><b>${esc(j.description)}</b>
        <span class="tag" style="color:${colour}">${esc(j.state)}</span>
        ${j.state === "running" ? ` ${pct}%` : ""}<br>
        <span class="mut">${esc(j.message)}${j.duration_s !== null
          ? ` · ${j.duration_s}s` : ""}${j.error ? " · " + esc(j.error) : ""}</span></div>
      <div>${["queued", "running"].includes(j.state)
        ? `<button onclick="jobCancel('${j.job_id}')">Cancel</button>` : ""}
        ${["failed", "cancelled"].includes(j.state)
        ? `<button onclick="jobRetry('${j.job_id}')">Retry</button>` : ""}</div>
    </div>`;
  }).join("") || '<span class="mut">no jobs</span>';
}
window.jobCancel = async (id) => {
  await api(`/api/jobs/${id}/cancel`, { method: "POST" }); refreshJobs();
};
window.jobRetry = async (id) => {
  await api(`/api/jobs/${id}/retry`, { method: "POST" }); refreshJobs();
};

// The chain the operator is editing. Held locally so the path can be
// reordered without a round trip per click, then pushed as one declaration.
let chainState = { tx_ids: [], rx_ids: [], antenna_tx: "", antenna_rx: "" };
let chainComps = [];

const compById = (id) => chainComps.find((x) => x.component_id === id);

function fmtLoss(c) {
  if (c.nominal_loss_db === null || c.nominal_loss_db === undefined) return null;
  return `${c.nominal_loss_db} dB`;
}

// One block in the signal-flow view. Amber border when the part has no
// measured loss, because that is exactly what the totals are missing.
function chainBlock(comp, kind) {
  if (!comp) return "";
  const loss = fmtLoss(comp);
  const cls = kind === "antenna" ? "antenna" : (loss === null ? "unchar" : "");
  const sub = kind === "antenna"
    ? (comp.has_vna || comp.vna ? "characterised" : "no VNA data")
    : (loss === null ? "loss unknown" : loss);
  return `<div class="chainblk ${cls}">
    <b>${esc(comp.name)}</b><span class="sub">${esc(comp.kind)} · ${esc(sub)}</span></div>`;
}

const ARROW = (t) => `<div class="chainarrow">${t}</div>`;

function renderChainFlow(c) {
  const radio = `<div class="chainblk radio"><b>Radio</b><span class="sub">RX / TX port</span></div>`;
  const rxAnt = compById(c.antenna_rx);
  const txAnt = compById(c.antenna_tx);
  let html = "";

  if (rxAnt || c.rx_path.length) {
    html += `<div class="subhead" style="margin-top:0">Receive</div><div class="chainflow">`;
    html += rxAnt ? chainBlock(rxAnt, "antenna")
                  : '<div class="chainblk unchar"><b>no antenna</b><span class="sub">not declared</span></div>';
    for (const comp of c.rx_path) { html += ARROW("→") + chainBlock(comp, comp.kind); }
    html += ARROW("→") + radio + `</div>`;
  }
  if (txAnt || c.tx_path.length) {
    html += `<div class="subhead">Transmit</div><div class="chainflow">` + radio;
    for (const comp of c.tx_path) { html += ARROW("→") + chainBlock(comp, comp.kind); }
    html += ARROW("→") + (txAnt ? chainBlock(txAnt, "antenna")
      : '<div class="chainblk unchar"><b>no antenna</b><span class="sub">not declared</span></div>');
    html += `</div>`;
  }
  if (!html) {
    html = '<div class="chainempty">Nothing declared yet — pick an antenna below and add cables.</div>';
  }
  $("chain-flow").innerHTML = html;
}

function renderChainFacts(c) {
  const band = c.band || {};
  const bands = (band.usable_bands || [])
    .map((b) => `<span class="tag" style="background:#12241d;border:1px solid #1f4a38;color:#86dfc0">${
      fmtHz(b.start_hz)}–${fmtHz(b.stop_hz)}</span>`).join(" ");
  let html = `<p class="mut" style="margin:8px 0 4px">`
    + `Total nominal loss <b>${c.total_loss_db} dB</b>`
    + ` · delay <b>${c.total_delay_ns} ns</b></p>`;
  html += `<p style="margin:4px 0"><span class="mut">Usable band:</span> `
    + (bands || '<span class="mut">unknown</span>') + `</p>`;
  for (const n of [c.note, band.note].filter(Boolean)) {
    html += `<p style="color:var(--warn);margin:4px 0">${esc(n)}</p>`;
  }
  $("chain-facts").innerHTML = html;

  const badge = c.config_name
    ? `<span class="tag">${esc(c.config_name)}</span>` +
      (c.config_modified ? '<span class="tag" style="background:#3a2f12;color:#e8c96a">modified</span>' : "")
    : '<span class="tag" style="background:#2a2f3a;color:#9aa8bd">unsaved</span>';
  $("chain-badge").innerHTML = badge;
}

// An RF path is a series of parts in a physical order. It is built by dragging
// inventory onto a port rather than picking from a list, because the thing an
// operator is describing is physical: this cable goes into that socket. The
// ports are drawn from the connected radio's channel counts, so a 2R2T board
// grows extra sockets without any change here.
function radioPorts() {
  const dev = (STATUS && STATUS.devices || []).find(
    (d) => d.connected && d.kind !== "simulated_pluto_plus")
    || (STATUS && STATUS.devices || []).find((d) => d.connected);
  const caps = (dev && dev.capabilities) || { rx_channels: 1, tx_channels: 1 };
  return {
    name: dev ? (dev.kind === "simulated_pluto_plus" ? "Simulated" : "Pluto") : "Radio",
    rx: Math.max(1, caps.rx_channels || 1),
    tx: Math.max(1, caps.tx_channels || 1),
  };
}

function chipHtml(c, opts = {}) {
  const loss = fmtLoss(c);
  const cls = [c.kind === "antenna" ? "antenna" : "",
               (c.kind !== "antenna" && loss === null) ? "unchar" : ""].join(" ");
  const sub = c.kind === "antenna"
    ? (c.has_vna ? "characterised" : "no VNA data")
    : (loss === null ? "loss unknown" : loss);
  return `<div class="chip ${cls}" draggable="true"
      data-cid="${esc(c.component_id)}" data-kind="${esc(c.kind)}"
      ${opts.from ? `data-from="${esc(opts.from)}" data-idx="${opts.idx}"` : ""}>
    <b>${esc(c.name)}${opts.from !== undefined && opts.from !== null
        ? `<span class="chipx" data-drop-cid="${esc(c.component_id)}"
             data-drop-from="${esc(opts.from)}" data-drop-idx="${opts.idx}"
             title="remove">✕</span>` : ""}</b>
    <span class="sub">${esc(c.kind)} · ${esc(sub)}</span>
  </div>`;
}

function portHtml(kind, n, total) {
  return `<div class="port"><b>${kind}${total > 1 ? n : ""}</b>
    <span class="sub">${kind === "RX" ? "receive" : "transmit"} port</span></div>`;
}

const ARROW_EL = (t) => `<div class="chainarrow">${t}</div>`;

function renderDropZone(which) {
  const host = $(`path-${which}`);
  if (!host) return;
  const ports = radioPorts();
  const ids = chainState[`${which}_ids`];
  const antId = chainState[which === "rx" ? "antenna_rx" : "antenna_tx"];
  const ant = antId ? compById(antId) : null;

  const antSlot = ant
    ? chipHtml(ant, { from: `${which}-antenna`, idx: 0 })
    : `<div class="slot">drop an antenna here</div>`;
  const parts = ids.map((id, i) => {
    const c = compById(id);
    if (!c) return `<div class="chip unchar"><b>missing</b>
        <span class="sub">${esc(id)}</span></div>`;
    return chipHtml(c, { from: which, idx: i });
  });
  const portBlocks = [];
  const count = which === "rx" ? ports.rx : ports.tx;
  for (let i = 1; i <= count; i++) {
    portBlocks.push(portHtml(which.toUpperCase(), i, count));
  }
  const portEl = portBlocks.join("");

  // signal order: RX runs antenna -> radio, TX runs radio -> antenna
  const seq = which === "rx"
    ? [antSlot, ...parts, portEl]
    : [portEl, ...parts.slice().reverse(), antSlot];
  host.innerHTML = seq.join(ARROW_EL("→")) ||
    '<span class="hint">drag a part here</span>';
  if (!ids.length && !ant) {
    host.insertAdjacentHTML("beforeend",
      ' <span class="hint" style="margin-left:10px">nothing patched yet</span>');
  }
}

function renderPalette() {
  const host = $("chain-palette");
  if (!host) return;
  host.innerHTML = chainComps.map((c) => chipHtml(c)).join("") ||
    '<span class="mut">no components yet — add them under Components below</span>';
  const pick = $("add-pick");
  if (pick) {
    pick.innerHTML = chainComps.map((c) =>
      `<option value="${esc(c.component_id)}">${esc(c.kind)}: ${esc(c.name)}</option>`)
      .join("") || '<option value="">add components first</option>';
  }
}

// --- drag plumbing ---------------------------------------------------------
document.addEventListener("dragstart", (ev) => {
  const chip = ev.target.closest && ev.target.closest(".chip");
  if (!chip) return;
  ev.dataTransfer.effectAllowed = "move";
  ev.dataTransfer.setData("text/plain", JSON.stringify({
    cid: chip.dataset.cid, kind: chip.dataset.kind,
    from: chip.dataset.from || "", idx: chip.dataset.idx }));
  chip.classList.add("dragging");
});
document.addEventListener("dragend", (ev) => {
  const chip = ev.target.closest && ev.target.closest(".chip");
  if (chip) chip.classList.remove("dragging");
});
document.addEventListener("dragover", (ev) => {
  const zone = ev.target.closest && ev.target.closest(".dropzone");
  if (!zone) return;
  ev.preventDefault();
  ev.dataTransfer.dropEffect = "move";
  zone.classList.add("over");
});
document.addEventListener("dragleave", (ev) => {
  const zone = ev.target.closest && ev.target.closest(".dropzone");
  if (zone && !zone.contains(ev.relatedTarget)) zone.classList.remove("over");
});
document.addEventListener("drop", (ev) => {
  const zone = ev.target.closest && ev.target.closest(".dropzone");
  if (!zone) return;
  ev.preventDefault();
  zone.classList.remove("over");
  let payload;
  try { payload = JSON.parse(ev.dataTransfer.getData("text/plain")); }
  catch (e) { return; }
  dropOnPath(zone.dataset.path, payload);
});

function removeFrom(from, idx, cid) {
  if (from === "rx" || from === "tx") {
    chainState[`${from}_ids`].splice(Number(idx), 1);
  } else if (from === "rx-antenna") {
    chainState.antenna_rx = "";
  } else if (from === "tx-antenna") {
    chainState.antenna_tx = "";
  }
}

function dropOnPath(which, { cid, kind, from, idx }) {
  if (from) removeFrom(from, idx, cid);
  if (kind === "antenna") {
    // One antenna per side: a port has a single thing screwed onto the end of
    // it, so dropping a second replaces the first rather than stacking.
    chainState[which === "rx" ? "antenna_rx" : "antenna_tx"] = cid;
  } else {
    chainState[`${which}_ids`].push(cid);
  }
  pushChain();
}

// removing a chip with the little x
document.addEventListener("click", (ev) => {
  const x = ev.target.closest && ev.target.closest(".chipx");
  if (!x) return;
  ev.preventDefault();
  ev.stopPropagation();
  removeFrom(x.dataset.dropFrom, x.dataset.dropIdx, x.dataset.dropCid);
  pushChain();
});

async function pushChain() {
  await api("/api/rf_chain", { method: "POST", body: chainState });
  refreshChain();
}

async function refreshChain() {
  try { chainComps = await api("/api/components"); } catch (e) { return; }
  const r = await api("/api/rf_chain");
  const c = r.resolved;
  chainState = {
    tx_ids: [...r.declared.tx_ids], rx_ids: [...r.declared.rx_ids],
    antenna_tx: r.declared.antenna_tx, antenna_rx: r.declared.antenna_rx,
  };

  // dashboard keeps a plain-language summary; the lab gets the visual
  if ($("chain-summary")) {
    const nm = (id) => { const x = compById(id); return x ? esc(x.name) : "—"; };
    $("chain-summary").innerHTML =
      (c.config_name ? `<b>${esc(c.config_name)}</b>${c.config_modified ? " (modified)" : ""}<br>` : "")
      + `RX: ${nm(c.antenna_rx)} → ${c.rx_path.map((x) => esc(x.name)).join(" → ") || "—"}<br>`
      + `TX: ${c.tx_path.map((x) => esc(x.name)).join(" ← ") || "—"} ← ${nm(c.antenna_tx)}<br>`
      + `total nominal loss ${c.total_loss_db} dB, delay ${c.total_delay_ns} ns`
      + (c.note ? `<br><span style="color:var(--warn)">${esc(c.note)}</span>` : "");
  }
  if (!$("chain-flow")) return;      // dashboard-only refresh

  renderChainFlow(c);
  renderChainFacts(c);
  renderPalette();
  renderDropZone("rx");
  renderDropZone("tx");
  await refreshChainConfigs();
}

async function refreshChainConfigs() {
  if (!$("chain-config-list")) return;
  let list;
  try { list = await api("/api/chains"); } catch (e) { return; }
  $("chain-config-list").innerHTML = list.map((c) => `
    <div class="row${c.active ? " on" : ""}">
      <span>${c.active ? "● " : ""}${esc(c.name)}
        <span class="mut">${c.measurement_count} measurement${
          c.measurement_count === 1 ? "" : "s"}</span></span>
      <span>
        <button onclick="activateChain('${esc(c.config_id)}')"${
          c.active ? " disabled" : ""}>Activate</button>
        <button onclick="deleteChain('${esc(c.config_id)}')">Delete</button>
      </span>
    </div>`).join("") ||
    '<span class="mut">no saved configurations yet — patch a chain, then name and save it</span>';

  const active = list.find((c) => c.active);
  if (!active) {
    $("chain-measurements").innerHTML =
      '<span class="mut">activate a configuration to see its measurements</span>';
    return;
  }
  const detail = await api(`/api/chains/${active.config_id}`);
  $("chain-measurements").innerHTML = (detail.measurements || []).map((m) => {
    if (m.missing) {
      return `<div class="row"><span>${esc(m.experiment_id)}</span>
        <span style="color:#e8c96a">capture deleted</span></div>`;
    }
    const s = m.summary || {};
    const band = s.start_hz
      ? `${(s.start_hz / 1e6).toFixed(0)}–${(s.stop_hz / 1e6).toFixed(0)} MHz`
      : "";
    const floor = s.median_noise_floor_dbfs !== undefined
      ? `median floor ${s.median_noise_floor_dbfs} dBFS` : "";
    return `<div class="row">
      <span><a href="#" onclick="openLibrary('${esc(m.experiment_id)}');return false">${
        esc(m.kind || "measurement")}</a>
        <span class="mut">${esc(band)}</span></span>
      <span class="mut">${esc(floor)}</span></div>`;
  }).join("") ||
    '<span class="mut">no measurements yet — run a band survey with this configuration active</span>';
}

window.activateChain = async (id) => {
  await api(`/api/chains/${id}/activate`, { method: "POST" });
  refreshChain();
};

window.deleteChain = async (id) => {
  if (!confirm("Delete this configuration? Measurements taken with it are kept.")) return;
  await api(`/api/chains/${id}/delete`, { method: "POST" });
  refreshChain();
};

// Keyboard/touch fallback: drag-and-drop is the fast path, not the only one.
$("add-go").onclick = () => {
  const cid = $("add-pick").value;
  if (!cid) return;
  const c = compById(cid);
  dropOnPath($("add-where").value, { cid, kind: c ? c.kind : "", from: "", idx: 0 });
};

$("chain-clear").onclick = () => {
  chainState = { tx_ids: [], rx_ids: [], antenna_tx: "", antenna_rx: "" };
  pushChain();
};

$("chain-detach").onclick = async () => {
  await api("/api/chains/detach", { method: "POST" });
  refreshChain();
};

$("chain-config-save").onclick = async () => {
  const name = $("chain-config-name").value.trim();
  if (!name) { alert("Give the configuration a name so it can be reused."); return; }
  try {
    await api("/api/chains", { method: "POST", body: { name } });
  } catch (e) {
    alert(`Could not save: ${e.message || e}`); return;
  }
  $("chain-config-name").value = "";
  refreshChain();
};

// Cross-page links. Delegated rather than bound once at load, because several
// of these are rendered into panels that redraw on every status poll.
document.addEventListener("click", (ev) => {
  const a = ev.target.closest("[data-goto]");
  if (!a) return;
  ev.preventDefault();
  const btn = document.querySelector(`nav button[data-tab="${a.dataset.goto}"]`);
  if (btn) btn.click();
});

$("atten-save").onclick = async () => {
  const declared = parseFloat($("atten-db").value) || 0;
  await api("/api/safety/path_attenuation", { method: "POST",
    body: { attenuation_db: declared } });
  // The edit is now the server's value, so it is no longer a pending edit;
  // clearing this lets the poll resume tracking the field.
  mirroredLastSynced["atten-db"] = String(declared);
  refreshStatus();
};

async function refreshRxProtection() {
  // Not devices[0] — that is always sim-pluto-0, registered first. This panel
  // was computing "estimated X dBm at the receive port" from the simulator's
  // gains while naming no device, so setting a real radio's TX gain changed
  // nothing here. Same hazard the device selectors were just fixed for, and
  // under-warning about the radio actually in use is the worse direction.
  const dev = (STATUS && preferredDevice(STATUS.devices)) || "";
  if (!dev) return;
  let c;
  try { c = await api(`/api/safety/rx_protection?device_id=${encodeURIComponent(dev)}`); }
  catch (e) { return; }

  // This check is a *prediction*: it assumes a transmit at full output with
  // whatever isolation has been declared. Until the platform is armed no such
  // transmit can happen, so at rest it is pre-flight advice, not an alarm.
  // Painting it red on an idle bench trains an operator to ignore red — which
  // is a way of hiding the problem, not surfacing it.
  const armed = !!(STATUS.safety && STATUS.safety.armed);
  const txLive = !!(STATUS.safety && STATUS.safety.tx_active);
  const imminent = armed || txLive;

  if (c.severity === "ok") {
    $("rx-protection").innerHTML =
      `<div class="mut">Receive path: estimated ${c.rx_input_dbm} dBm at the port
       if you transmit — within safe limits (damage above
       ${c.thresholds.damage_dbm} dBm).</div>`;
    return;
  }
  const cls = imminent ? (c.severity === "critical" ? "err" : "warn") : "warn";
  const head = imminent
    ? (c.severity === "critical" ? "Receiver at risk — TX is live"
                                 : "Receive path warning — TX is live")
    : "Before you transmit";
  // Name the radio. The figures come from one device's configuration and the
  // bench has more than one, so an unattributed "this configuration" invites
  // reading a simulator's numbers as the bench's.
  const lead = imminent
    ? `estimated ${c.rx_input_dbm} dBm at the port of <code>${esc(dev)}</code>.`
    : `<code>${esc(dev)}</code>'s configuration would put an estimated
       ${c.rx_input_dbm} dBm at the receive port. Nothing is transmitting, so
       nothing is at risk yet.`;
  $("rx-protection").innerHTML =
    `<div class="alert ${cls}"><b>${esc(head)}</b> — ${lead}<br>
     ${c.warnings.map(esc).join("<br>")}
     <br><span class="mut">Declare the isolation between TX and RX in
     <a href="#" data-goto="hardware">Hardware</a>.</span></div>`;
}

// Things that are wrong *now*, as opposed to things that would be wrong if you
// transmitted. Kept separate so a prediction never masquerades as a fault.
function renderAttention(s) {
  if (!$("dash-attention")) return;
  const items = [];
  if (!s.devices.some((d) => d.connected && d.kind !== "simulated_pluto_plus")) {
    items.push(["warn", `No radio is connected. <a href="#" data-goto="hardware">Set one up</a>.`]);
  }
  if (s.storage && s.storage.low_space_warning) {
    items.push(["err", "Disk space is low; captures are immutable and accumulate."]);
  }
  $("dash-attention").innerHTML = items.map(
    ([c, t]) => `<div class="alert ${c}">${t}</div>`).join("");
}

/* ---------- hardware rescan ---------- */
async function doRescan(uri) {
  // Measuring every transport takes a few seconds, so say so rather than
  // leaving a button that looks like it did nothing.
  $("rescan-status").textContent = uri
    ? `connecting to ${uri}…` : "measuring each way in…";
  try {
    const r = await api("/api/devices/rescan", { method: "POST", body: { uri } });
    if (!r.driver.available) { $("rescan-status").textContent = r.driver.detail; return; }
    const bits = [];
    for (const d of r.added) {
      const L = d.link || {};
      bits.push(`found a radio over ${L.kind === "network" ? "Ethernet" : L.kind}` +
                (L.address ? ` at ${L.address}` : "") +
                (L.throughput_mb_s ? ` (${L.throughput_mb_s} MB/s)` : ""));
    }
    if (r.already_present.length) bits.push("already listed: " + r.already_present.join(", "));
    r.errors.forEach((e) => bits.push(`${e.uri}: ${e.error}`));
    $("rescan-status").textContent = bits.join(" · ") || "no radios found";
    refreshStatus();
  } catch (e) { $("rescan-status").textContent = "scan failed: " + e.message; }
}
$("rescan-btn").onclick = () => doRescan("");

$("radio-add").onclick = async () => {
  const address = $("radio-addr").value.trim();
  if (!address) { alert("Enter the radio's hostname or IP address."); return; }
  try {
    await api("/api/radios", { method: "POST", body: {
      address, label: $("radio-label").value.trim() } });
  } catch (e) { alert(`Could not save that address: ${e.message || e}`); return; }
  $("radio-addr").value = ""; $("radio-label").value = "";
  await refreshRadioBook();
  doRescan("");          // saving an address is a request to go and look
};

/* ---------- Live RF ---------- */
let waterfallRows = [];

// Show what the radio is actually set to, not what the boxes were last left
// at. These are editable, so they are only synced on connect and on selecting
// a device — never per frame, which would fight the operator mid-keystroke.
// A field that mirrors server state has to be *loaded* from it, not only
// written back to it. These were populated from hardcoded values in the HTML
// and refreshed only on connect, device-change and apply — never on page
// load. So a refresh showed 61.44 MSPS and 56 MHz against a radio running
// 30.72/30.72, and pressing "Apply config" without touching anything would
// have pushed those defaults onto the radio. One place defines the mapping
// now, so a new field cannot be added to the form and forgotten here.
const CFG_FIELDS = [
  ["cfg-freq", (c) => (c.center_frequency_hz / 1e6).toFixed(3).replace(/\.?0+$/, "")],
  ["cfg-rate", (c) => (c.sample_rate_hz / 1e6).toFixed(2)],
  // Not toFixed(0): a radio at 30.72 MHz rendered as "31", and pressing
  // Apply without touching anything then pushed 31 MHz onto it. A field that
  // cannot represent the value it was loaded with corrupts it on round trip.
  ["cfg-bw", (c) => (c.rx_bandwidth_hz / 1e6).toFixed(2).replace(/\.?0+$/, "")],
  ["cfg-rxgain", (c) => String(c.rx_gain_db)],
  ["cfg-txgain", (c) => String(c.tx_gain_db)],
];

function deviceFromStatus(id) {
  return ((STATUS && STATUS.devices) || []).find((x) => x.device_id === id);
}

async function syncDeviceConfigInputs(id) {
  let d = deviceFromStatus(id);
  if (!d) {
    try { d = ((await api("/api/status")).devices || []).find((x) => x.device_id === id); }
    catch (e) { return; }
  }
  if (!d || !d.config) return;
  for (const [field, read] of CFG_FIELDS) $(field).value = read(d.config);
  renderConfigDrift(id);
}

// Loading on boot fixes the stale form, but it cannot stop the radio moving
// afterwards — another page, a bench script, or the operator part-way through
// an edit. Rather than overwrite what someone is typing on a 5 s poll, say
// when the form and the radio disagree and offer to reload. Silent
// disagreement between two views of the same radio is the actual complaint.
function renderConfigDrift(id) {
  const el = $("cfg-drift");
  if (!el) return;
  const d = deviceFromStatus(id || ($("live-device") || {}).value);
  if (!d || !d.config) { el.innerHTML = ""; return; }
  const differs = CFG_FIELDS.filter(([f, read]) => $(f).value !== read(d.config));
  if (!differs.length) { el.innerHTML = ""; return; }
  el.innerHTML = `<span class="tag" style="background:#2b2410;border:1px solid #5c4c1c;
    color:#e8c96a">form differs from the radio: ${
      differs.map(([f]) => esc(f.replace("cfg-", ""))).join(", ")}</span>
    <button onclick="reloadConfigForm()">Load from radio</button>`;
}

window.reloadConfigForm = () => syncDeviceConfigInputs($("live-device").value);

$("live-connect").onclick = async () => {
  const id = $("live-device").value;
  try {
    await api(`/api/devices/${id}/connect`, { method: "POST" });
    $("live-status").textContent = "connected";
    $("live-stream").disabled = false;
    $("live-tx").disabled = false;
    $("live-record").disabled = false;
    await syncDeviceConfigInputs(id);
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
    await syncDeviceConfigInputs(id);
    $("live-status").textContent = "config applied";
  } catch (e) { $("live-status").textContent = "rejected: " + e.message; }
};

$("live-device").onchange = () => syncDeviceConfigInputs($("live-device").value);

$("live-stream").onclick = () => {
  if (ws) { ws.close(); ws = null; $("live-stream").textContent = "Start stream"; return; }
  const id = $("live-device").value;
  liveAlerts.clear();
  paintLiveAlerts();
  if (!window._liveAlertTimer) window._liveAlertTimer = setInterval(paintLiveAlerts, 1000);
  ws = new WebSocket(
    `ws://${location.host}/ws/live?device_id=${encodeURIComponent(id)}&fps=6`);
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

// Live alerts are held, counted and timestamped rather than redrawn from the
// current frame alone (UX-LIVE-005).
//
// Measured on the bench radio: clipping fired on 1 frame in 87 and
// near-clipping on 2. Because this function used to overwrite innerHTML every
// frame, each of those was on screen for a single frame interval — about
// 0.3 s — then erased by the next clean frame. Long enough to catch a flash of
// colour, far too short to read. An intermittent fault is the kind that matters
// most, so a fired alert now stays up, says how often it has happened and how
// long ago, and expires only after the condition has been quiet for a while.
const LIVE_ALERT_HOLD_MS = 20000;
const liveAlerts = new Map();

function noteAlert(key, cls, text) {
  const now = Date.now();
  const a = liveAlerts.get(key);
  if (a) { a.count++; a.last = now; a.text = text; }
  else liveAlerts.set(key, { cls, text, count: 1, last: now });
}

function paintLiveAlerts() {
  if (!$("live-alerts")) return;
  const now = Date.now();
  const rows = [];
  for (const [k, a] of liveAlerts) {
    if (now - a.last > LIVE_ALERT_HOLD_MS) { liveAlerts.delete(k); continue; }
    const ago = Math.round((now - a.last) / 1000);
    rows.push(`<div class="alert ${a.cls}">${esc(a.text)}` +
      `<span class="mut"> · ${a.count} time${a.count === 1 ? "" : "s"}` +
      `, last ${ago}s ago</span></div>`);
  }
  $("live-alerts").innerHTML = rows.join("");
}

function renderLiveAlerts(f) {
  const q = f.quality || {};
  // peak_amplitude is linear against full scale, so 1.0 is the ADC ceiling.
  const peak = q.peak_amplitude > 0
    ? ` · peak ${(20 * Math.log10(q.peak_amplitude)).toFixed(1)} dBFS` : "";
  const gain = f.config && f.config.rx_gain_db !== undefined
    ? `, RX gain ${f.config.rx_gain_db} dB` : "";
  if (f.clipped) {
    noteAlert("clip", "err",
      `Receiver clipping — samples are being lost at the ADC. Reduce RX gain${gain}${peak}`);
  } else if (q.near_clipping) {
    noteAlert("near", "warn", `Signal near full scale${gain}${peak}`);
  }
  if (f.loss_events && f.loss_events.length) {
    noteAlert("loss", "err",
      `Sample loss: ${f.loss_events.length} event(s) — recorded, not concealed`);
  }
  paintLiveAlerts();
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
let lastPeaks = [];

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
    lastPeaks = r.peaks;
    drawRangeProfile(r.range_profile, r.peaks);
    refreshPriorRuns();
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
  if (priorProfile && priorProfile.ranges_m) {
    // a prior run is a separate measurement, so it gets its own colour and
    // is drawn on the same range axis for honest comparison
    ctx.strokeStyle = "#4aa3ff"; ctx.lineWidth = 1.2; ctx.setLineDash([4, 3]);
    ctx.beginPath();
    priorProfile.magnitude_db.forEach((v, i) => {
      const rr = priorProfile.ranges_m[i];
      if (rr > ranges[n - 1]) return;
      const px = (rr / ranges[n - 1]) * (cv.width - 50) + 40;
      i ? ctx.lineTo(px, Y(v)) : ctx.moveTo(px, Y(v));
    });
    ctx.stroke(); ctx.setLineDash([]);
    ctx.fillStyle = "#8db8e8"; ctx.font = "11px monospace";
    ctx.fillText("prior: " + priorProfile.label, 48, 20);
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

/* prior-run overlay (UX-RNG-005) */
let priorProfile = null;

async function refreshPriorRuns() {
  try {
    const runs = await api("/api/experiments?kind=range");
    $("range-prior").innerHTML = '<option value="">none</option>' +
      runs.slice(0, 30).map((e) =>
        `<option value="${esc(e.experiment_id)}">${esc(e.name)} — ${esc(e.experiment_id)}</option>`).join("");
  } catch (e) { /* library may be empty */ }
}

$("range-prior-load").onclick = async () => {
  const id = $("range-prior").value;
  if (!id) return;
  try {
    const d = await api(`/api/experiments/${id}/derived/range_profile`);
    priorProfile = d.product.range_profile;
    priorProfile.label = id;
    $("range-status").textContent = "overlaying " + id;
    if (lastProfile) drawRangeProfile(lastProfile, lastPeaks);
  } catch (e) { $("range-status").textContent = "overlay failed: " + e.message; }
};
$("range-prior-clear").onclick = () => {
  priorProfile = null;
  if (lastProfile) drawRangeProfile(lastProfile, lastPeaks);
};

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
/* position source (survey wheel) */
async function refreshPositionUi() {
  try {
    const ports = await api("/api/position/ports");
    $("pos-port").innerHTML = '<option value="">—</option>' +
      ports.ports.map((p) =>
        `<option value="${esc(p.device)}">${esc(p.device)} — ${esc(p.description)}</option>`).join("");
    const st = await api("/api/position");
    const l = st.latest;
    $("pos-status").textContent =
      `${st.kind}${st.port ? " " + st.port : ""}` +
      (l ? ` · at ${l.x_m.toFixed(3)} m ±${l.uncertainty_m} m` +
           (l.stale_s > 1 ? ` (stale ${l.stale_s}s)` : "") : " · no reading") +
      (st.bad_lines ? ` · ${st.bad_lines} bad line(s)` : "");
    $("scan-next-auto").disabled = !(currentScan && l);
  } catch (e) { /* position rig is optional */ }
}

$("pos-apply").onclick = async () => {
  const kind = $("pos-kind").value;
  const body = { kind };
  if (kind === "serial") {
    body.port = $("pos-port").value;
    body.wheel_circumference_m = parseFloat($("pos-circ").value) || 0;
    body.counts_per_revolution = parseInt($("pos-cpr").value, 10) || 0;
    if (!body.port) { alert("choose a serial port"); return; }
  }
  try {
    await api("/api/position/source", { method: "POST", body });
    refreshPositionUi();
  } catch (e) { $("pos-status").textContent = "failed: " + e.message; }
};

$("scan-next-auto").onclick = async () => {
  if (!currentScan) return;
  try {
    const r = await api(`/api/scan/${currentScan.id}/point`, {
      method: "POST", body: { use_position_source: true } });
    if (!r.accepted) {
      $("scan-status").textContent = "rejected: " + r.gate_failures.join(", ");
      return;
    }
    $("scan-status").textContent =
      `captured at ${r.progress ? "grid point" : ""} (wheel)`;
    await renderBScan();
  } catch (e) { $("scan-status").textContent = "failed: " + e.message; }
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
  refreshChain();
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
  const c = await api(`/api/components/${id}`);
  $("comp-edit").style.display = "";

  const vna = c.vna;
  const s21 = vna && vna.s21_analysis;
  const row = (k, v, unknown) =>
    `<dt>${esc(k)}</dt><dd class="${unknown ? "unknown" : ""}">${v}</dd>`;
  const val = (x, fallback) => (x === null || x === undefined || x === ""
    ? `<span class="mut">${fallback}</span>` : esc(String(x)));

  let html = `<h3 style="margin:0 0 6px">${esc(c.name)}
    <span class="tag">${esc(c.kind)}</span>
    ${c.connector ? `<span class="tag">${esc(c.connector)}</span>` : ""}</h3><dl class="kv">`;
  html += row("Claimed band", val(c.claimed_band, "not stated"));
  if (c.kind === "antenna") html += row("Polarization", val(c.polarization, "not stated"));
  html += row("Loss", c.nominal_loss_db === null || c.nominal_loss_db === undefined
    ? '<span class="unknown">not characterised</span>' : `${c.nominal_loss_db} dB`,
    c.nominal_loss_db === null || c.nominal_loss_db === undefined);
  html += row("Delay", c.nominal_delay_ns === null || c.nominal_delay_ns === undefined
    ? '<span class="unknown">not characterised</span>' : `${c.nominal_delay_ns} ns`,
    c.nominal_delay_ns === null || c.nominal_delay_ns === undefined);
  html += row("Added", new Date(c.created_at * 1000).toLocaleString());

  if (vna) {
    const b = vna.analysis.best_match;
    // Say where it came from, rather than defaulting to "imported" — an
    // instrument sweep has no filename, so this used to label a live sweep
    // as an import and contradict the provenance line lower down the panel.
    const src = vna.source || {};
    const origin = vna.filename ? esc(vna.filename)
      : src.kind === "instrument" ? `swept on ${esc(src.instrument || "a VNA")}`
      : "origin not recorded";
    html += row("VNA sweep", `${origin} · ${vna.ports}-port · ` +
      `${vna.freqs_hz.length} points, ${fmtHz(vna.freqs_hz[0])}–${fmtHz(vna.freqs_hz[vna.freqs_hz.length - 1])}`);
    html += row("Best match", `VSWR ${b.vswr} at ${fmtHz(b.freq_hz)} (S11 ${b.s11_db} dB)`);
    if (s21) {
      html += row("Insertion loss",
        `${s21.at_lowest.loss_db} dB at ${fmtHz(s21.at_lowest.freq_hz)} → ` +
        `${s21.at_highest.loss_db} dB at ${fmtHz(s21.at_highest.freq_hz)}`);
    }
  } else {
    html += row("VNA sweep", '<span class="mut">none imported</span>');
  }
  if (c.notes) html += row("Notes", esc(c.notes).replace(/\n/g, "<br>"));
  html += `</dl>`;
  $("comp-card").innerHTML = html;

  $("comp-loss").value = c.nominal_loss_db ?? "";
  $("comp-delay").value = c.nominal_delay_ns ?? "";
  $("comp-notes").value = c.notes || "";
  // Only offer to adopt a measurement that actually exists.
  $("comp-adopt-loss").style.display = s21 ? "" : "none";

  // Delay is only offerable when the stored sweep can actually support one —
  // an open port 2 gives a phase slope through noise, which is not a delay.
  const da = vna && vna.delay_analysis;
  $("comp-adopt-delay").style.display = (da && da.usable) ? "" : "none";
  const hint = $("comp-delay-hint");
  if (da) {
    hint.style.display = "";
    hint.firstElementChild.innerHTML = da.usable
      ? `Stored sweep gives ${da.delay_ns} ns from S21 phase slope. `
        + `Single-sweep only — valid if the true delay is under `
        + `${da.unambiguous_max_ns} ns, which this has not checked. `
        + `<b>Measure delay</b> below sweeps twice and settles it.`
      : esc(da.note);
  } else {
    hint.style.display = "none";
  }

  renderBands(c);
  $("vna-panel").style.display = vna ? "" : "none";
  if (vna) drawVnaPlot(c, pinnedComp);
  refreshComponents();
};

$("comp-save-char").onclick = async () => {
  if (!selectedComp) return;
  const num = (id) => ($(id).value === "" ? null : Number($(id).value));
  await api(`/api/components/${selectedComp}/update`, { method: "POST", body: {
    nominal_loss_db: num("comp-loss"), nominal_delay_ns: num("comp-delay") } });
  openComponent(selectedComp);
  refreshChain();
};

$("comp-save-notes").onclick = async () => {
  if (!selectedComp) return;
  await api(`/api/components/${selectedComp}/update`,
            { method: "POST", body: { notes: $("comp-notes").value } });
  openComponent(selectedComp);
};

$("comp-adopt-loss").onclick = async () => {
  if (!selectedComp) return;
  try {
    await api(`/api/components/${selectedComp}/adopt_loss`, { method: "POST", body: {} });
  } catch (e) { alert(e.message || e); return; }
  openComponent(selectedComp);
  refreshChain();
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
  $("comp-card").innerHTML = '<span class="mut">select a component</span>';
  $("comp-edit").style.display = "none";
  $("comp-bands").innerHTML = "";
  $("vna-panel").style.display = "none";
  refreshComponents();
  refreshChain();       // the deleted part may have been in the active chain
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

// -- VNA instrument ---------------------------------------------------------
// The VNA is not gated by SafetyController: its source is fixed-level and
// free-runs whenever the instrument is powered, so there is nothing for a TX
// fingerprint to bind to. Every sweep is audited and its span checked against
// the active profile, and an out-of-profile sweep is surfaced here rather
// than blocked — see runtime._vna_band_check.

function vnaNotice(html, tone) {
  const colors = {
    warn: "#2b2410;border:1px solid #5c4c1c;color:#e8c96a",
    bad: "#1c1116;border:1px solid #3a2028;color:#a06070",
    ok: "#12241d;border:1px solid #1f4a38;color:#86dfc0",
  };
  $("vna-notice").innerHTML = html
    ? `<p class="tag" style="background:${colors[tone] || colors.warn};
       display:block;padding:6px 8px">${html}</p>` : "";
}

async function vnaDetect() {
  try {
    const s = await api("/api/vna/status");
    $("vna-instrument").textContent =
      `${s.model} fw ${s.firmware} — ${s.frequency_range}` +
      (s.battery_mv ? ` — ${(s.battery_mv / 1000).toFixed(2)} V` : "");
    const cal = s.calibration;
    // Never render this as "calibrated" full stop: the firmware reports which
    // standards are captured but not the span they cover.
    $("vna-cal").textContent = cal.applied
      ? `cal: ${cal.standards.join(" ")} applied — span unverified`
      : "cal: none applied";
    $("vna-points").innerHTML = [101, 201, 301]
      .filter((p) => p <= s.max_points)
      .map((p) => `<option>${p}</option>`).join("");
    vnaNotice("", "ok");
    return s;
  } catch (e) {
    $("vna-instrument").textContent = "instrument not detected";
    $("vna-cal").textContent = "";
    vnaNotice(`Could not reach the VNA: ${esc(String(e.message || e))}`, "bad");
    return null;
  }
}

$("vna-detect").onclick = vnaDetect;

$("vna-sweep").onclick = async () => {
  if (!selectedComp) { alert("select a component first"); return; }
  const body = {
    start_hz: Number($("vna-start").value) * 1e6,
    stop_hz: Number($("vna-stop").value) * 1e6,
    points: Number($("vna-points").value),
    ports: Number($("vna-ports").value),
    comp_id: selectedComp,
  };
  const btn = $("vna-sweep");
  btn.disabled = true; btn.textContent = "Sweeping…";
  try {
    const r = await api("/api/vna/sweep", { method: "POST", body });
    const msgs = [];
    if (r.band_check && !r.band_check.inside_profile) {
      msgs.push(esc(r.band_check.warning));
    }
    msgs.push("Calibration span is unverified — run <b>Check calibration</b> "
      + "against a known thru to establish whether the instrument's "
      + "calibration covers this span.");
    vnaNotice(msgs.join("<br><br>"), "warn");
    openComponent(selectedComp);
  } catch (e) {
    vnaNotice(`Sweep failed: ${esc(String(e.message || e))}`, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "Sweep into this component";
  }
};

$("vna-delay").onclick = async () => {
  if (!selectedComp) { alert("select a component first"); return; }
  const body = {
    start_hz: Number($("vna-start").value) * 1e6,
    stop_hz: Number($("vna-stop").value) * 1e6,
    points_a: 101, points_b: 301,
    comp_id: selectedComp,
    reference_plane_ns: Number($("vna-refplane").value) || 0,
  };
  const btn = $("vna-delay");
  btn.disabled = true; btn.textContent = "Measuring… (two sweeps)";
  try {
    const r = await api("/api/vna/measure_delay", { method: "POST", body });
    const cc = r.cross_check;
    if (cc.agree) {
      let msg = `<b>Electrical delay ${r.total_delay_ns} ns</b> — written to `
        + `this component.<br>Cross-checked at ${cc.compared[0].points} and `
        + `${cc.compared[1].points} points, agreeing to ${cc.difference_ns} ns, `
        + `so the phase did not alias.`;
      if (r.reference_plane_ns) {
        msg += `<br><span class="mut">Measured ${r.delay_ns} ns plus `
          + `${r.reference_plane_ns} ns you declared for the calibration `
          + `reference plane — that part is an assumption, not a measurement.`
          + `</span>`;
      }
      vnaNotice(msg, "ok");
    } else {
      // Disagreement means at least one sweep wrapped, so the true delay is
      // longer than both. Storing the smaller number would be inventing one.
      vnaNotice(`<b>Delay not established</b><br>${esc(cc.note)}<br>`
        + `<span class="mut">Nothing was written to the component.</span>`,
        "warn");
    }
    openComponent(selectedComp);
  } catch (e) {
    vnaNotice(`Delay measurement failed: ${esc(String(e.message || e))}`, "bad");
  } finally {
    btn.disabled = false;
    btn.textContent = "Measure delay into this component";
  }
};

$("comp-adopt-delay").onclick = async () => {
  if (!selectedComp) return;
  const ref = Number($("vna-refplane").value) || 0;
  try {
    await api(`/api/components/${selectedComp}/adopt_delay`,
      { method: "POST", body: { reference_plane_ns: ref } });
    openComponent(selectedComp);
  } catch (e) {
    alert(String(e.message || e));
  }
};

$("vna-calcheck").onclick = async () => {
  const body = {
    start_hz: Number($("vna-start").value) * 1e6,
    stop_hz: Number($("vna-stop").value) * 1e6,
    points: Number($("vna-points").value),
  };
  const btn = $("vna-calcheck");
  btn.disabled = true; btn.textContent = "Measuring…";
  try {
    const r = await api("/api/vna/calibration_check", { method: "POST", body });
    const res = r.residual;
    vnaNotice(
      `<b>${res.covers_span ? "Calibration covers this span" : "Calibration is "
        + "not established for this span"}</b><br>${esc(res.verdict)}<br>`
      + `residual max ${res.max_deviation_db} dB, mean ${res.mean_deviation_db} dB `
      + `(edges ${res.edge_mean_db} dB vs mid-band ${res.mid_mean_db} dB)<br>`
      + `<span class="mut">Assumes a known thru was connected — the instrument `
      + `cannot see the port.</span>`,
      res.covers_span ? "ok" : "warn");
  } catch (e) {
    vnaNotice(`Calibration check failed: ${esc(String(e.message || e))}`, "bad");
  } finally {
    btn.disabled = false; btn.textContent = "Check calibration";
  }
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

  const notes = [];

  // A two-port sweep measures S11 *through* the component's own loss, and
  // loss attenuates any reflection twice. A 6 dB pad shows a near-perfect
  // VSWR and is a useless feedline. Measured on this bench: an 8 in low-loss
  // thru read -16 dB at 2.5 GHz while a 5.8 dB cable read -24 dB mean on the
  // same instrument — the lossier part looked better matched. Presenting the
  // rating unqualified here invites exactly the wrong conclusion.
  if (c.vna.ports === 2) {
    notes.push('<span class="mut">These ratings come from S11 measured '
      + 'through this part’s own loss, which attenuates any reflection '
      + 'twice and flatters the match. For a two-port part, read the '
      + 'insertion loss above; the VSWR rating describes an antenna, not a '
      + 'cable.</span>');
  }

  // Provenance, never defaulted to something reassuring.
  const src = c.vna.source || { kind: "unknown" };
  const cal = c.vna.calibration || { known: false };
  const origin = src.kind === "instrument"
    ? `swept on ${esc(src.instrument || "a VNA")}`
    : src.kind === "file" ? `imported from ${esc(src.filename || "a file")}`
      : "origin not recorded";
  const calText = cal.known
    ? "calibration verified for this span"
    : "calibration span unverified";
  notes.push(`<span class="mut">${origin} — <b>${calText}</b>. `
    + `${esc(cal.note || "")}</span>`);

  $("comp-bands").innerHTML = `<p>${chips}</p>`
    + notes.map((n) => `<p>${n}</p>`).join("");
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
