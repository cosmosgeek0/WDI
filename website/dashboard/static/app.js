"use strict";

let rangeSeconds = 900;
let lastStatus = {};
let lastSeries = {points: []};
let lastBoxes = {boxes: []};
let lastEvents = {events: [], counts: {}};
let refreshInFlight = false;

const $ = id => document.getElementById(id);
function formatBytes(value) {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let amount = Math.max(0, Number(value) || 0), unit = 0;
  while (amount >= 1000 && unit < units.length - 1) { amount /= 1000; unit += 1; }
  const decimals = amount >= 100 || unit === 0 ? 0 : amount >= 10 ? 1 : 2;
  return `${amount.toFixed(decimals)} ${units[unit]}`;
}
const formatRate = value => `${formatBytes(value)}/s`;
const utc = ms => new Date(ms).toISOString().replace("T", " ").replace(/\.\d{3}Z$/, " UTC");
function age(ms) {
  if (!ms) return "—";
  const seconds = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  return `${Math.floor(seconds / 3600)}h ago`;
}
function duration(ms) {
  if (ms == null) return "waiting";
  const seconds = Math.max(0, Number(ms) / 1000);
  if (seconds < 60) return `${seconds.toFixed(seconds < 10 ? 1 : 0)}s`;
  return `${(seconds / 60).toFixed(1)}m`;
}
function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function setupCanvas(canvas) {
  const ratio = window.devicePixelRatio || 1;
  const width = Math.max(320, canvas.clientWidth);
  const height = parseFloat(getComputedStyle(canvas).height) || 280;
  canvas.width = Math.round(width * ratio);
  canvas.height = Math.round(height * ratio);
  const ctx = canvas.getContext("2d");
  ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
  return {ctx, width, height};
}
function drawTraffic() {
  const canvas = $("trafficChart"), {ctx, width, height} = setupCanvas(canvas);
  const points = lastSeries.points || [];
  ctx.clearRect(0, 0, width, height);
  if (points.length < 2) {
    ctx.fillStyle = "#667085";
    ctx.font = "13px -apple-system, sans-serif";
    ctx.fillText("Waiting for one-second samples…", 40, 42);
    return;
  }
  const box = {left: 82, right: 18, top: 14, bottom: 30};
  const values = points.map(point => point.eth0.rx_bytes_per_second + point.eth0.tx_bytes_per_second);
  const maximum = Math.max(1, ...values);
  const first = points[0].time_ms, last = points[points.length - 1].time_ms;
  const span = Math.max(1, last - first);
  const x = time => box.left + ((time - first) / span) * (width - box.left - box.right);
  const y = value => box.top + (height - box.top - box.bottom) * (1 - value / maximum);
  ctx.font = "11px ui-monospace, monospace";
  ctx.textAlign = "right";
  ctx.textBaseline = "middle";
  for (let step = 0; step <= 4; step += 1) {
    const value = maximum * step / 4, yPos = y(value);
    ctx.strokeStyle = "#e5e9ef"; ctx.lineWidth = 1;
    ctx.beginPath(); ctx.moveTo(box.left, yPos); ctx.lineTo(width - box.right, yPos); ctx.stroke();
    ctx.fillStyle = "#667085"; ctx.fillText(formatRate(value), box.left - 8, yPos);
  }
  const expectedReporters = Number(lastBoxes.expected_reporter_count || 10);
  const expectedStep = Number(lastSeries.bucket_seconds || 1) * 1000;
  ctx.strokeStyle = "#2463eb"; ctx.lineWidth = 1.8; ctx.beginPath();
  let drawing = false;
  let previousTime = null;
  points.forEach((point, index) => {
    const complete = (point.instance_ids || []).length === expectedReporters
      && Number(point.sample_count || 0) === expectedReporters * Number(lastSeries.bucket_seconds || 1)
      && Number(point.partial_intervals || 0) === 0;
    const contiguous = previousTime == null || point.time_ms - previousTime <= expectedStep * 1.5;
    if (!complete || !contiguous) {
      if (drawing) ctx.stroke();
      drawing = false;
      ctx.fillStyle = "#e66a2c";
      ctx.fillRect(x(point.time_ms) - 1.5, y(values[index]) - 1.5, 3, 3);
      ctx.beginPath();
      previousTime = point.time_ms;
      return;
    }
    if (!drawing) { ctx.moveTo(x(point.time_ms), y(values[index])); drawing = true; }
    else ctx.lineTo(x(point.time_ms), y(values[index]));
    previousTime = point.time_ms;
  });
  if (drawing) ctx.stroke();
  ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillStyle = "#667085";
  for (let step = 0; step <= 5; step += 1) {
    const time = first + span * step / 5;
    ctx.fillText(new Date(time).toISOString().slice(11, 19), x(time), height - box.bottom + 8);
  }
}

function renderBoxes() {
  const table = $("boxTable"); table.replaceChildren();
  const boxes = [...(lastBoxes.boxes || [])].sort((a, b) => Number(b.is_live) - Number(a.is_live) || b.last_sample_ms - a.last_sample_ms);
  if (!boxes.length) {
    const row = document.createElement("tr");
    const cell = element("td", "", "No recorder samples yet."); cell.colSpan = 16; row.append(cell); table.append(row);
  }
  for (const box of boxes) {
    const latest = box.snapshot || {rx_bytes_per_second: 0, tx_bytes_per_second: 0, bytes_per_second: 0};
    const average = box.observed_average || {bytes_per_second: 0};
    const peak = box.peak || {bytes_per_second: 0, time_ms: null};
    const honeygain = box.honeygain || {};
    const state = box.is_live ? (honeygain.state || "unknown") : "stale";
    const stateCell = document.createElement("td"); stateCell.append(element("span", `state ${state}`, state.toUpperCase()));
    const connections = `TCP ${honeygain.tcp_established_count || 0}/${honeygain.tcp_socket_count || 0} established · UDP ${honeygain.udp_socket_count || 0}`;
    const comparison = box.slowdown_ratio == null ? "—" : `${(box.slowdown_ratio * 100).toFixed(0)}%`;
    const completeness = `${Number(box.completeness_percent || 0).toFixed(1)}% (${box.sample_count || 0}/${box.expected_sample_count || 0}; ${box.partial_interval_count || 0} partial)`;
    const totalMoved = (box.totals?.rx_bytes || 0) + (box.totals?.tx_bytes || 0);
    const row = document.createElement("tr");
    for (const cell of [
      element("td", "", (box.machine_id || box.instance_id).slice(0, 8)),
      element("td", "", utc(box.first_seen_ms || box.first_sample_ms)),
      stateCell, element("td", "", connections),
      element("td", "", box.is_live ? formatRate(latest.rx_bytes_per_second) : "—"),
      element("td", "", box.is_live ? formatRate(latest.tx_bytes_per_second) : "—"),
      element("td", "", box.is_live ? formatRate(latest.bytes_per_second) : "—"),
      element("td", "", formatRate(average.bytes_per_second)), element("td", "", formatRate(peak.bytes_per_second)),
      element("td", "", peak.time_ms ? utc(peak.time_ms) : "—"),
      element("td", "", formatRate(box.first_5m_average_bytes_per_second || 0)),
      element("td", "", formatRate(box.last_5m_average_bytes_per_second || 0)), element("td", "", comparison),
      element("td", "", completeness), element("td", "", formatBytes(totalMoved)), element("td", "", age(box.last_sample_ms)),
    ]) row.append(cell);
    table.append(row);
  }
  const live = boxes.filter(box => box.is_live && box.snapshot);
  const rx = live.reduce((sum, box) => sum + box.snapshot.rx_bytes_per_second, 0);
  const tx = live.reduce((sum, box) => sum + box.snapshot.tx_bytes_per_second, 0);
  $("currentRate").textContent = formatRate(rx + tx);
  $("currentSplit").textContent = `RX ${formatRate(rx)} · TX ${formatRate(tx)}`;
  $("reportingBoxes").textContent = `${lastBoxes.snapshot_reporter_count || 0} / ${lastBoxes.expected_reporter_count || 10}`;
  $("activeApps").textContent = `${live.filter(box => box.honeygain?.state === "up").length} / ${lastBoxes.expected_reporter_count || 10}`;
}

function renderEvents() {
  const labels = {start:"START", authenticated:"AUTHENTICATED", service_starting:"SERVICE STARTING", connected:"CONNECTED", disconnected:"DISCONNECTED", network_overused:"NETWORK OVERUSED", duplicate_device:"DUPLICATE DEVICE", device_limit_exceeded:"DEVICE LIMIT", unusable_network:"UNUSABLE NETWORK", authentication_error:"AUTH ERROR", api_ping_error:"API PING ERROR", exit:"EXIT", stopping:"STOPPING", stopped:"STOPPED"};
  const table = $("eventTable"); table.replaceChildren();
  for (const event of (lastEvents.events || []).slice(0, 250)) {
    const row = document.createElement("tr");
    for (const cell of [element("td", "", utc(event.event_time_ms)), element("td", "", event.machine_id.slice(0, 8)), element("td", "", event.client_version || "monitor"), element("td", "", labels[event.event] || event.event), element("td", "", String(event.attempt || "—")), element("td", "", event.event === "exit" ? String(event.exit_code) : "—")]) row.append(cell);
    table.append(row);
  }
  const summary = Object.entries(lastEvents.counts || {}).map(([event, count]) => `${labels[event] || event}: ${count}`).join(" · ");
  $("eventSummary").textContent = summary || "No Honeygain status events in this window yet.";
}

function renderSource() {
  const list = $("sourceStatus"); list.replaceChildren();
  const add = (term, value) => list.append(element("dt", "", term), element("dd", "", value));
  add("Poller", lastStatus.poll_state || "unknown");
  const source = lastStatus.source || {};
  add("Source", source.organization ? `${source.organization} / ${source.project} / ${source.container_group}` : "waiting");
  add("Deployment version", source.version || "waiting");
  add("Capture", lastStatus.capture_mode || "waiting");
  add("Stored samples", Number(lastStatus.sample_count || 0).toLocaleString());
  add("Source lag", duration(lastStatus.source_lag_ms));
  add("Tail fetch", lastStatus.tail_fetch_interval_seconds ? `${lastStatus.tail_fetch_interval_seconds}s cadence` : "waiting");
  add("Last API success", lastStatus.last_success_ms ? utc(lastStatus.last_success_ms) : "waiting");
  add("Settled through", lastStatus.settled_through_ms ? utc(lastStatus.settled_through_ms) : "waiting");
  add("Database", formatBytes(lastStatus.database_size_bytes || 0));
}

function requestWindow() {
  const liveEnd = Date.now() + 1000, liveStart = liveEnd - rangeSeconds * 1000;
  const bucket = rangeSeconds <= 900 ? 1 : rangeSeconds <= 3600 ? 2 : rangeSeconds <= 21600 ? 15 : 60;
  const seriesEnd = bucket >= 60 ? Math.floor(liveEnd / 60000) * 60000 : liveEnd;
  const seriesStart = seriesEnd - rangeSeconds * 1000;
  const liveQuery = `start=${Math.floor(liveStart)}&end=${Math.floor(liveEnd)}&bucket=1`;
  const seriesQuery = `start=${Math.floor(seriesStart)}&end=${Math.floor(seriesEnd)}&bucket=${bucket}`;
  $("csvExport").href = `/api/export.csv?${seriesQuery}`;
  $("jsonExport").href = `/api/export.json?${seriesQuery}`;
  $("utcWindow").textContent = `${new Date(liveStart).toISOString().slice(11, 19)} → now UTC`;
  return {liveQuery, seriesQuery};
}
async function refresh() {
  if (refreshInFlight || document.getElementById("hg1Panel").classList.contains("hidden")) return;
  refreshInFlight = true;
  try {
    const {liveQuery, seriesQuery} = requestWindow();
    const responses = await Promise.all(["/api/status", `/api/series?${seriesQuery}`, `/api/boxes?${liveQuery}`, `/api/events?${liveQuery}`].map(url => fetch(url, {cache:"no-store"})));
    if (responses.some(response => !response.ok)) throw new Error("Local API unavailable");
    [lastStatus, lastSeries, lastBoxes, lastEvents] = await Promise.all(responses.map(response => response.json()));
    if ($("hg1Panel").classList.contains("hidden")) return;
    renderBoxes(); drawTraffic(); renderEvents(); renderSource();
    $("sampleAge").textContent = age(lastStatus.last_sample_ms);
    $("sampleTime").textContent = lastStatus.last_sample_ms ? utc(lastStatus.last_sample_ms) : "waiting for data";
    const liveCount = (lastBoxes.boxes || []).filter(box => box.is_live).length;
    const warnings = [];
    if (lastStatus.poll_state === "error") warnings.push(lastStatus.poll_error || lastStatus.last_error || "Ingestion failed.");
    const expectedCount = lastBoxes.expected_reporter_count || 10;
    if (liveCount !== expectedCount) warnings.push(`${liveCount} of ${expectedCount} boxes have a fresh recorder sample.`);
    $("notice").textContent = warnings.join(" ");
    $("notice").classList.toggle("hidden", warnings.length === 0);
    $("connection").className = `connection ${warnings.length ? "error" : "ok"}`;
    $("connectionText").textContent = warnings.length ? "Incomplete" : `${liveCount} / ${expectedCount} live`;
  } catch (error) {
    if ($("hg1Panel").classList.contains("hidden")) return;
    $("connection").className = "connection error";
    $("connectionText").textContent = "Dashboard unavailable";
    $("notice").textContent = String(error.message || error);
    $("notice").classList.remove("hidden");
  } finally {
    refreshInFlight = false;
  }
}

document.querySelectorAll("[data-range]").forEach(button => button.addEventListener("click", () => {
  document.querySelectorAll("[data-range]").forEach(other => other.classList.toggle("active", other === button));
  rangeSeconds = Number(button.dataset.range); refresh();
}));
window.addEventListener("resize", () => { if (!$("hg1Panel").classList.contains("hidden")) drawTraffic(); });
window.addEventListener("dashboardchange", event => { if (event.detail === "HG1") refresh(); });
refresh();
setInterval(refresh, 1000);
