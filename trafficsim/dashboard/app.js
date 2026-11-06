// trafficsim dashboard: plain JS + Chart.js, no build step.
const $ = (id) => document.getElementById(id);
const fmt = (n) => (n == null ? "-" : Math.round(n).toLocaleString());
const MAX_POINTS = 900;
const COLORS = ["#0b6e69", "#c26a1b", "#4c6ed6", "#9c3d8f", "#5b8c2a", "#b8323a", "#6b5b45", "#2b8fb3", "#8a8f2a", "#d1477f"];

let meta = null;
let current = null;          // latest run status
let yamlDirty = false;
let paused = false;
let lastRaw = null;

// ------------------------------------------------------------------ charts
function lineChart(el, opts = {}) {
  return new Chart($(el), {
    type: "line",
    data: { labels: [], datasets: [] },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, normalized: true,
      interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0 }, line: { borderWidth: 1.6, tension: 0.15 } },
      scales: {
        x: { ticks: { maxTicksLimit: 8, autoSkip: true } },
        y: { beginAtZero: true, stacked: !!opts.stacked, ticks: { callback: (v) => Number(v).toLocaleString() } },
        ...(opts.y2 ? { y2: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false } } } : {}),
      },
      plugins: { legend: { labels: { boxWidth: 10, font: { size: 11 } } } },
    },
  });
}
const charts = {};
function initCharts() {
  const ink = getComputedStyle(document.documentElement).getPropertyValue("--muted").trim();
  Chart.defaults.color = ink;
  Chart.defaults.borderColor = getComputedStyle(document.documentElement).getPropertyValue("--line").trim();
  charts.active = lineChart("c-active", { stacked: true });
  charts.type = lineChart("c-type");
  charts.sdk = lineChart("c-sdk", { y2: true });
}

function dataset(chart, label, opts = {}) {
  let ds = chart.data.datasets.find((d) => d.label === label);
  if (!ds) {
    const color = opts.color || COLORS[chart.data.datasets.length % COLORS.length];
    ds = { label, data: new Array(chart.data.labels.length).fill(null), borderColor: color,
           backgroundColor: opts.fill ? color + "55" : color, fill: !!opts.fill, ...opts.extra };
    chart.data.datasets.push(ds);
  }
  return ds;
}

function pushPoint(p, update = true) {
  const label = (p.sim_time_iso || "").slice(5, 19).replace("T", " ");
  const push = (chart, values) => {
    chart.data.labels.push(label);
    const seen = new Set();
    for (const [name, v, opts] of values) {
      dataset(chart, name, opts || {}).data.push(v);
      seen.add(name);
    }
    for (const ds of chart.data.datasets) if (!seen.has(ds.label)) ds.data.push(null);
    if (chart.data.labels.length > MAX_POINTS) {
      chart.data.labels.shift();
      chart.data.datasets.forEach((d) => d.data.shift());
    }
    if (update) chart.update("none");
  };
  const act = Object.keys(p.active_by || {}).sort().map((k) => [k, p.active_by[k], { fill: true }]);
  act.push(["target", p.target, { color: "#888", extra: { borderDash: [5, 4], fill: false, stack: "target", yAxisID: "y" } }]);
  push(charts.active, act);
  push(charts.type, Object.keys(p.eps_by_type || {}).sort().map((k) => [k, p.eps_by_type[k]]));
  push(charts.sdk, [
    ["sent/s", p.sdk_sent_rate, { color: "#0b6e69" }],
    ["failed/s", p.sdk_failed_rate, { color: "#b8323a" }],
    ["dropped/s", p.sdk_dropped_rate, { color: "#c26a1b" }],
    ["queue depth", (p.sdk || {}).queue_depth, { color: "#888", extra: { yAxisID: "y2", borderDash: [3, 3] } }],
  ]);
}

function resetCharts() {
  for (const c of Object.values(charts)) { c.data.labels = []; c.data.datasets = []; c.update("none"); }
}

// ------------------------------------------------------------------ headline / panels
function renderPoint(p) {
  $("empty").hidden = true;
  $("live").hidden = false;
  $("h-active").textContent = fmt(p.active);
  $("h-target").textContent = fmt(p.target);
  $("h-active").closest(".lead").classList.toggle("on-target", p.target > 0 && Math.abs(p.active - p.target) / p.target < 0.1);
  const iso = p.sim_time_iso || "";
  $("h-clock").textContent = iso ? `${iso.slice(5, 10)} ${iso.slice(11, 19)} UTC` : "-";
  $("h-clock").title = iso;
  $("h-speed").textContent = `${p.speed}x${p.paused ? " (paused)" : ""}`;
  $("h-eps").textContent = fmt(p.eps);
  $("h-sent").textContent = fmt(p.sdk_sent_rate);
  const sdk = p.sdk || {};
  $("h-queue").textContent = fmt(sdk.queue_depth);
  $("h-fail").textContent = `${fmt(sdk.failed)} / ${fmt(sdk.dropped)}`;
  $("h-lag").textContent = `${fmt(p.lag)}s`;
  $("h-total").textContent = fmt(p.total_events);
  const warn = [...((current && current.warnings) || [])];
  if (sdk.failed > 0) warn.push(`${fmt(sdk.failed)} events failed to upload: check the collector host and write keys.`);
  if (sdk.dropped > 0) warn.push(`${fmt(sdk.dropped)} events were dropped because an SDK queue was full: lower speed/concurrency or raise sdk.max_queue_size / sdk.threads.`);
  if (p.lag > 120) warn.push(`Workers are ${fmt(p.lag)} simulated seconds behind the clock. Event timestamps stay correct, but delivery is late: lower speed or add workers.`);
  $("warn").hidden = !warn.length;
  $("warn").textContent = warn.join(" ");
  const ov = p.overlays || [];
  $("overlays").textContent = ov.length ? "Active events: " + ov.map((o) => `${o.name} (${Math.round(o.intensity * 100)}%)`).join(", ") : "";
  $("top").innerHTML = (p.top_events || []).map(([n, v]) => `<li><span>${escapeHtml(n)}</span><span>${fmt(v)}/s</span></li>`).join("");
  if (p.funnels) renderFunnels(p.funnels);
  if (p.state) setRunState(p.state);
}

function renderFunnels(f) {
  $("funnels").innerHTML = Object.entries(f).map(([name, steps]) => `
    <div class="funnel"><h3>${escapeHtml(name)}</h3>
    ${steps.map((s) => `<div class="step"><span>${escapeHtml(s.step)}</span>
      <span class="track"><span class="fill" data-w="${Math.min(100, Number(s.rate) * 100).toFixed(1)}"></span></span>
      <span class="num">${(s.rate * 100).toFixed(1)}% · ${fmt(s.count)}</span></div>`).join("")}
    </div>`).join("") || `<p class="muted">Funnels appear once events flow.</p>`;
  // widths are set through the CSSOM because the Content-Security-Policy forbids inline style attributes
  for (const el of $("funnels").querySelectorAll(".fill[data-w]")) el.style.width = `${el.dataset.w}%`;
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

function setRunState(state) {
  const live = ["running", "paused", "stopping"].includes(state);
  $("pause").disabled = !["running", "paused"].includes(state);
  $("stop").disabled = !live;
  $("apply-speed").disabled = !["running", "paused"].includes(state);
  $("trigger").disabled = !["running", "paused"].includes(state);
  $("start").disabled = live || state === "finishing";
  paused = state === "paused";
  $("pause").textContent = paused ? "Resume" : "Pause";
  const hasRun = !!current;
  for (const id of ["dl-summary", "dl-csv"]) {
    $(id).classList.toggle("disabled", !hasRun);
    $(id).setAttribute("aria-disabled", String(!hasRun));
  }
  if (current) $("h-run").textContent = `run ${current.run_id} · ${current.scenario} · ${state} · ${current.workers} workers`;
}

function msg(text, error = false) {
  $("msg").textContent = text;
  $("msg").classList.toggle("error", error);
}

// ------------------------------------------------------------------ scenario form
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ") : res.statusText);
  }
  return body;
}

async function loadScenario(id) {
  const s = await api(`/api/scenarios/${encodeURIComponent(id)}`);
  lastRaw = s.raw;
  yamlDirty = false;
  $("yaml").value = s.yaml;
  const r = s.raw;
  $("scenario-desc").textContent = r.description || "";
  $("p-concurrency").value = r.traffic?.base_concurrency ?? 1000;
  $("p-speed").value = r.speed ?? 1;
  $("p-duration").value = r.duration ?? "";
  $("p-start").value = r.start_at ?? "now";
  $("p-seed").value = r.seed ?? 42;
  $("p-workers").value = r.workers ?? "";
  $("p-cap").value = r.events_per_sec_cap ?? "";
  $("p-sample").value = r.sample_payloads ?? "";
  const rows = [];
  for (const [v, vc] of Object.entries(r.verticals || {})) {
    if (vc && vc.enabled === false) continue;
    const mix = (vc && vc.platforms) || meta.default_platform_mix[v];
    if (!meta.verticals[v]) continue;
    const e = escapeHtml;
    const cells = meta.verticals[v].map((p) => `<input type="number" min="0" step="0.05" data-v="${e(v)}" data-p="${e(p)}" value="${e(Number(mix[p] ?? 0))}" aria-label="${e(v)} ${e(p)} weight" title="${e(p)}">`);
    rows.push(`<div class="mix-row"><span>${e(v)}</span>${cells.join("")}${meta.verticals[v].length < 3 ? "<span></span>" : ""}</div>
      <div class="mix-row muted"><span></span>${meta.verticals[v].map((p) => `<span>${e(p)}</span>`).join("")}</div>`);
  }
  $("mix-rows").innerHTML = rows.join("") || `<p class="muted small">No verticals enabled in this scenario.</p>`;
}

function collectRequest() {
  const o = {};
  const num = (id) => ($(id).value === "" ? null : Number($(id).value));
  o["traffic.base_concurrency"] = num("p-concurrency");
  o["speed"] = num("p-speed");
  o["duration"] = $("p-duration").value || null;
  o["start_at"] = $("p-start").value || null;
  o["seed"] = num("p-seed");
  o["workers"] = num("p-workers");
  o["events_per_sec_cap"] = num("p-cap");
  o["sample_payloads"] = num("p-sample");
  for (const inp of document.querySelectorAll("#mix-rows input")) {
    o[`verticals.${inp.dataset.v}.platforms.${inp.dataset.p}`] = Number(inp.value || 0);
  }
  for (const k of Object.keys(o)) if (o[k] === null || Number.isNaN(o[k])) delete o[k];
  // Once the YAML has been edited it is the source of truth: the form still shows the
  // scenario's original values, so sending them as overrides would undo the edits.
  if (yamlDirty) return { yaml: $("yaml").value, overrides: {}, dry_run: $("dry").checked };
  return { scenario: $("scenario").value, overrides: o, dry_run: $("dry").checked };
}

// ------------------------------------------------------------------ websocket
function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => { $("conn").textContent = "live"; $("conn").dataset.state = "up"; };
  ws.onclose = () => { $("conn").textContent = "disconnected, retrying"; $("conn").dataset.state = "down"; setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "hello") {
      resetCharts();
      if (m.status) {
        current = m.status;
        m.series.forEach((p) => pushPoint(p, false));
        Object.values(charts).forEach((c) => c.update("none"));
        if (m.series.length) renderPoint({ ...m.series[m.series.length - 1], funnels: m.status.funnels });
        setRunState(m.status.state);
      }
    } else if (m.type === "started") {
      current = m.status;
      resetCharts();
      setRunState(m.status.state);
    } else if (m.type === "point") {
      if (current && m.run_id !== current.run_id) return;
      pushPoint(m);
      renderPoint(m);
    } else if (m.type === "finished") {
      setRunState("finished");
      const s = m.summary || {};
      msg(`Run finished: ${fmt(s.total_events)} events, peak ${fmt(s.peak_active_sessions)} sessions, avg ${fmt(s.avg_eps)} events/s.`);
      loadRuns();
    }
  };
}

async function loadRuns() {
  const runs = await api("/api/runs").catch(() => []);
  $("past-runs").innerHTML = runs.filter((r) => r.files.length).slice(0, 8).map((r) =>
    `<li>${escapeHtml(r.id)}: ${r.files.map((f) => `<a href="/api/runs/${encodeURIComponent(r.id)}/files/${encodeURIComponent(f)}">${escapeHtml(f)}</a>`).join(", ")}</li>`).join("");
}

// ------------------------------------------------------------------ wiring
async function main() {
  initCharts();
  meta = await api("/api/meta");
  $("dest").textContent = `collector: ${meta.destination.host}`;
  const list = await api("/api/scenarios");
  $("scenario").innerHTML = list.map((s) => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.id)}</option>`).join("");
  $("ev-type").innerHTML = Object.entries(meta.event_types).map(([k, v]) => `<option value="${escapeHtml(k)}">${escapeHtml(k)}${v ? ` (${escapeHtml(v)})` : ""}</option>`).join("");
  if (list.length) await loadScenario(list[0].id);
  $("scenario").onchange = () => loadScenario($("scenario").value);
  $("yaml").oninput = () => { yamlDirty = true; };

  $("validate").onclick = async () => {
    try {
      const r = await api("/api/validate", { method: "POST", body: JSON.stringify(collectRequest()) });
      msg(`Valid. Planned peak ≈ ${fmt(r.peak)} concurrent sessions across ${r.cohorts} cohorts.`);
    } catch (e) { msg(e.message, true); }
  };
  $("start").onclick = async () => {
    $("start").disabled = true;
    msg("Starting workers…");
    try {
      current = await api("/api/runs", { method: "POST", body: JSON.stringify(collectRequest()) });
      resetCharts();
      setRunState(current.state);
      msg(`Run ${current.run_id} started with ${current.workers} workers (planned peak ≈ ${fmt(current.planned_peak)} sessions).`);
    } catch (e) { msg(e.message, true); $("start").disabled = false; }
  };
  $("pause").onclick = async () => {
    try { current = await api(`/api/runs/current/${paused ? "resume" : "pause"}`, { method: "POST" }); setRunState(current.state); }
    catch (e) { msg(e.message, true); }
  };
  $("stop").onclick = async () => {
    msg("Stopping: draining SDK queues, then writing exports…");
    try { current = await api("/api/runs/current/stop", { method: "POST" }); setRunState(current.state); }
    catch (e) { msg(e.message, true); }
  };
  $("apply-speed").onclick = async () => {
    const speed = Number($("live-speed").value);
    if (!(speed > 0)) return msg("Enter a speed greater than 0.", true);
    try { current = await api("/api/runs/current/speed", { method: "POST", body: JSON.stringify({ speed }) }); msg(`Speed set to ${speed}x.`); }
    catch (e) { msg(e.message, true); }
  };
  $("trigger").onclick = async () => {
    const spec = { type: $("ev-type").value };
    if ($("ev-mag").value) spec.magnitude = Number($("ev-mag").value);
    if ($("ev-hold").value) spec.hold = $("ev-hold").value;
    if (!meta.event_types[spec.type]) return msg("The custom event type needs a vertical; define it in scenario YAML instead.", true);
    try { const ov = await api("/api/runs/current/events", { method: "POST", body: JSON.stringify(spec) }); msg(`Triggered ${ov.name} (x${ov.magnitude}) for ${ov.vertical}.`); }
    catch (e) { msg(e.message, true); }
  };
  $("dl-summary").href = "/api/runs/current/export/summary.json";
  $("dl-csv").href = "/api/runs/current/export/timeseries.csv";
  connect();
  loadRuns();
}

main().catch((e) => msg(`Failed to load: ${e.message}`, true));
