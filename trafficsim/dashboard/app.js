// trafficsim dashboard: plain JS + Chart.js, no build step.
// The Content-Security-Policy forbids inline scripts and style attributes: all styling is in
// styles.css, and the few dynamic sizes are set through the CSSOM (element.style).
"use strict";

const $ = (id) => document.getElementById(id);
const fmt = (n) => (n == null || Number.isNaN(Number(n)) ? "–" : Math.round(n).toLocaleString());
const MAX_POINTS = 900;
const COLORS = ["#0b6e69", "#c26a1b", "#4c6ed6", "#9c3d8f", "#5b8c2a", "#b8323a", "#6b5b45", "#2b8fb3", "#8a8f2a", "#d1477f"];

const VERTICAL = { streaming: "Streaming", ecom: "E-commerce", gaming: "Gaming" };
const PLATFORM = { web: "Web", ios: "iOS", android: "Android", pc: "PC", ps5: "PS5" };
const CALL_TYPE = {
  track: "Actions (track)", page: "Web pages (page)", screen: "App screens (screen)",
  identify: "Sign-ins (identify)", alias: "Sign-ups (alias)", group: "Groups (group)",
};
const EVENT_INFO = {
  premiere: "A new title is released and viewers flock to it.",
  live_event: "A live broadcast: viewers arrive fast and stay for hours.",
  flash_sale: "A short sale: more shoppers, and more of them check out.",
  black_friday: "Several days of heavier shopping.",
  campaign_push: "A push notification brings a burst of iOS and Android users.",
  game_launch: "A huge wave of new players installing and playing for the first time.",
  patch_day: "Players log in at once after an update.",
  tournament: "A matchmaking surge on PC and PS5.",
  weekend_event: "More play across a weekend.",
};
const EVENT_NAME = { black_friday: "Black Friday" };
const eventLabel = (k) => EVENT_NAME[k] || sentence(k);
const STATE_LABEL = {
  created: "Starting…", running: "Running", paused: "Paused", stopping: "Stopping…",
  finishing: "Saving exports…", finished: "Finished",
};
const ACTIVE_STATES = ["created", "running", "paused", "stopping", "finishing"];

let meta = null;
let current = null;       // latest run status from the server
let lastPoint = null;
let yamlDirty = false;
let lastCheck = null;     // result of "Check settings" for the current form values
let stopArmed = null;

// ------------------------------------------------------------------ text helpers
function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}
const sentence = (s) => { const t = String(s).replace(/[_-]+/g, " ").trim(); return t.charAt(0).toUpperCase() + t.slice(1); };
const vertLabel = (v) => VERTICAL[v] || sentence(v);
const platLabel = (p) => PLATFORM[p] || sentence(p);
const cohortLabel = (k) => { const [v, p] = String(k).split("/"); return p ? `${vertLabel(v)} · ${platLabel(p)}` : vertLabel(v); };

/** "90m", "1h30m", "2d", "45s" or a number of seconds -> seconds (NaN if unparseable). */
function parseDuration(text) {
  const s = String(text ?? "").trim().toLowerCase();
  if (s === "") return NaN;
  if (/^\d+(\.\d+)?$/.test(s)) return Number(s);
  const unit = { s: 1, m: 60, h: 3600, d: 86400, w: 604800 };
  let total = 0;
  let rest = s;
  for (const m of s.matchAll(/(\d+(?:\.\d+)?)\s*([smhdw])/g)) { total += Number(m[1]) * unit[m[2]]; rest = rest.replace(m[0], ""); }
  return rest.trim() === "" && total > 0 ? total : NaN;
}

function humanDuration(sec) {
  if (!Number.isFinite(sec)) return "–";
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return `${sec} s`;
  const d = Math.floor(sec / 86400), h = Math.floor((sec % 86400) / 3600), m = Math.floor((sec % 3600) / 60);
  if (d) return h ? `${d} d ${h} h` : `${d} day${d === 1 ? "" : "s"}`;
  if (h) return m ? `${h} h ${m} min` : `${h} hour${h === 1 ? "" : "s"}`;
  return `${m} min`;
}

function simClock(iso) {
  if (!iso) return "–";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString(undefined, { timeZone: "UTC", weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" }) + " UTC";
}

// ------------------------------------------------------------------ charts
const charts = {};

function lineChart(el, opts = {}) {
  return new Chart($(el), {
    type: "line",
    data: { labels: [], datasets: [] },
    options: {
      animation: false, responsive: true, maintainAspectRatio: false, normalized: true,
      interaction: { mode: "index", intersect: false },
      elements: { point: { radius: 0 }, line: { borderWidth: 1.8, tension: 0.2 } },
      scales: {
        x: { ticks: { maxTicksLimit: 6, autoSkip: true, maxRotation: 0 }, grid: { display: false } },
        y: { beginAtZero: true, stacked: !!opts.stacked, ticks: { callback: (v) => Number(v).toLocaleString() } },
        ...(opts.y2 ? { y2: { position: "right", beginAtZero: true, grid: { drawOnChartArea: false },
          title: { display: true, text: "waiting in queue" } } } : {}),
      },
      plugins: { legend: { position: "bottom", labels: { boxWidth: 10, boxHeight: 10, font: { size: 11 } } } },
    },
  });
}

function initCharts() {
  const css = getComputedStyle(document.documentElement);
  Chart.defaults.color = css.getPropertyValue("--muted").trim();
  Chart.defaults.borderColor = css.getPropertyValue("--line").trim();
  Chart.defaults.font.family = css.getPropertyValue("--font").trim();
  charts.active = lineChart("c-active", { stacked: true });
  charts.type = lineChart("c-type");
  charts.sdk = lineChart("c-sdk", { y2: true });
}

function dataset(chart, label, opts = {}) {
  let ds = chart.data.datasets.find((d) => d.label === label);
  if (!ds) {
    const color = opts.color || COLORS[chart.data.datasets.length % COLORS.length];
    ds = { label, data: new Array(chart.data.labels.length).fill(null), borderColor: color,
           backgroundColor: opts.fill ? color + "40" : color, fill: !!opts.fill, ...opts.extra };
    chart.data.datasets.push(ds);
  }
  return ds;
}

function pushPoint(p, update = true) {
  const label = (p.sim_time_iso || "").slice(11, 16);
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
  const act = Object.keys(p.active_by || {}).sort().map((k) => [cohortLabel(k), p.active_by[k], { fill: true }]);
  act.push(["Target", p.target, { color: "#8a8f94", extra: { borderDash: [5, 4], fill: false, stack: "target" } }]);
  push(charts.active, act);
  push(charts.type, Object.keys(p.eps_by_type || {}).sort().map((k) => [CALL_TYPE[k] || k, p.eps_by_type[k]]));
  push(charts.sdk, [
    ["Sent", p.sdk_sent_rate, { color: "#0b6e69" }],
    ["Failed", p.sdk_failed_rate, { color: "#b8323a" }],
    ["Dropped", p.sdk_dropped_rate, { color: "#c26a1b" }],
    ["Waiting in queue", (p.sdk || {}).queue_depth, { color: "#8a8f94", extra: { yAxisID: "y2", borderDash: [3, 3] } }],
  ]);
}

function resetCharts() {
  for (const c of Object.values(charts)) { c.data.labels = []; c.data.datasets = []; c.update("none"); }
}

// ------------------------------------------------------------------ live view
function showLive() {
  $("empty").hidden = true;
  $("live").hidden = false;
}

function renderPoint(p) {
  lastPoint = p;
  showLive();
  const sdk = p.sdk || {};
  const dry = !!(current && current.dry_run);

  $("k-active").textContent = fmt(p.active);
  const pct = p.target > 0 ? Math.round((p.active / p.target) * 100) : null;
  $("k-active-sub").textContent = p.target > 0 ? `Target ${fmt(p.target)} · ${pct}%` : "Target 0";

  $("k-eps").textContent = fmt(p.eps);
  $("k-eps-sub").textContent = dry ? "Practice run: generated, not sent" : `${fmt(p.sdk_sent_rate)} per second reaching the collector`;

  $("k-sent").closest(".kpi").querySelector("h3").textContent = dry ? "Events generated" : "Events delivered";
  if (dry) {
    $("k-sent").textContent = fmt(p.total_events);
    $("k-sent-sub").textContent = "Generated so far (practice run)";
  } else {
    $("k-sent").textContent = fmt(sdk.sent);
    const lost = [`${fmt(sdk.failed)} failed`, `${fmt(sdk.dropped)} dropped`];
    if (sdk.oversize > 0) lost.push(`${fmt(sdk.oversize)} too large`);
    $("k-sent-sub").textContent = `${lost.join(" · ")} · ${fmt(sdk.queue_depth)} waiting`;
  }

  $("k-clock").textContent = simClock(p.sim_time_iso);
  $("k-clock").title = p.sim_time_iso || "";
  const bits = [`${p.speed}× speed`];
  if (p.paused) bits.push("paused");
  if (p.lag > 5) bits.push(`${humanDuration(p.lag)} behind`);
  $("k-clock-sub").textContent = bits.join(" · ");

  renderProgress(p);
  renderHealth(p);

  const ov = p.overlays || [];
  $("overlays").innerHTML = ov.map((o) =>
    `<li><span class="pulse" aria-hidden="true"></span>${escapeHtml(eventLabel(o.name))} <span class="hint">${Math.round(o.intensity * 100)}% strength</span></li>`).join("");

  const top = p.top_events || [];
  const maxTop = top.length ? top[0][1] : 0;
  $("top").innerHTML = top.length ? top.map(([n, v]) => {
    const [vert, name] = String(n).split(": ");
    return `<li><span class="name">${escapeHtml(name || vert)} <span class="hint">${escapeHtml(name ? vertLabel(vert) : "")}</span></span>
      <span class="bar"><span class="fill" data-w="${maxTop ? ((v / maxTop) * 100).toFixed(1) : 0}"></span></span>
      <span class="num">${fmt(v)}/s</span></li>`;
  }).join("") : `<li class="hint">Waiting for events…</li>`;
  applyWidths($("top"));
  if (p.funnels) renderFunnels(p.funnels);
  if (current && p.speed) current.speed = p.speed;
  if (p.state) setRunState(p.state);
}

function renderProgress(p) {
  const s = current && current.sim_start, e = current && current.sim_end;
  const t = p.sim_time;
  if (s == null || e == null || t == null) {
    $("r-progress").style.width = "0%";
    $("progress-wrap").classList.add("open-ended");
    $("r-progress-text").textContent = "Runs until you stop it";
    $("r-progressbar").removeAttribute("aria-valuenow");
    return;
  }
  $("progress-wrap").classList.remove("open-ended");
  const frac = Math.min(1, Math.max(0, (t - s) / (e - s)));
  $("r-progress").style.width = `${(frac * 100).toFixed(1)}%`;
  $("r-progressbar").setAttribute("aria-valuenow", String(Math.round(frac * 100)));
  const state = p.state || (current && current.state);
  let text = `${Math.round(frac * 100)}% of ${humanDuration(e - s)} simulated`;
  if (state === "running" && frac < 1 && p.speed > 0) text += ` · about ${humanDuration((e - t) / p.speed + (p.lag || 0) / p.speed)} left`;
  $("r-progress-text").textContent = text;
}

function renderHealth(p) {
  const sdk = p.sdk || {};
  const state = p.state || (current && current.state);
  const dry = !!(current && current.dry_run);
  let level = "ok";
  let text;
  if (!dry && (sdk.failed > 0 || sdk.dropped > 0 || sdk.oversize > 0 || (state === "finished" && sdk.undelivered > 0))) {
    level = "bad";
    const parts = [];
    if (state === "finished" && sdk.undelivered > 0) parts.push(`${fmt(sdk.undelivered)} were still queued when the run ended`);
    if (sdk.failed > 0) parts.push(`${fmt(sdk.failed)} failed (check the collector address and write keys)`);
    if (sdk.oversize > 0) parts.push(`${fmt(sdk.oversize)} discarded for exceeding the SDK's 32 KB message limit (shorten the event's properties)`);
    if (sdk.dropped > 0) parts.push(`${fmt(sdk.dropped)} dropped because a send queue was full (slow the run down or raise sdk.max_queue_size)`);
    text = `Some events are not reaching the collector: ${parts.join("; ")}.`;
  } else if (state === "finished") {
    text = dry ? "Practice run finished. Nothing was sent." : "Run finished. Every event was delivered.";
  } else if (p.paused) {
    level = "info";
    text = "Paused. No new events are generated until you resume.";
  } else if (p.lag > 120) {
    level = "warn";
    text = `Falling behind by ${humanDuration(p.lag)} of simulated time. Timestamps stay correct, but events arrive late. Lower the speed or add worker processes.`;
  } else {
    text = dry ? "Practice run: events are generated but not sent." : "Healthy. All events are reaching the collector.";
  }
  $("health").dataset.level = level;
  $("health").textContent = text;
  const notes = (current && current.warnings) || [];
  $("notices").innerHTML = notes.map((w) => `<li>${escapeHtml(w)}</li>`).join("");
}

function renderFunnels(f) {
  const entries = Object.entries(f);
  $("funnels").innerHTML = entries.map(([name, steps]) => {
    const [vert, fname] = name.split(".");
    return `<div class="funnel"><h4>${escapeHtml(vertLabel(vert))} · ${escapeHtml(sentence(fname || ""))}</h4>
    ${steps.map((s) => `<div class="step"><span class="name">${escapeHtml(sentence(s.step))}</span>
      <span class="bar"><span class="fill" data-w="${Math.min(100, Number(s.rate) * 100).toFixed(1)}"></span></span>
      <span class="num">${(s.rate * 100).toFixed(1)}%</span></div>`).join("")}
    </div>`;
  }).join("") || `<p class="hint">Funnels appear once events flow.</p>`;
  applyWidths($("funnels"));
}

function applyWidths(root) {
  for (const el of root.querySelectorAll(".fill[data-w]")) el.style.width = `${el.dataset.w}%`;
}

function setRunState(state) {
  const live = ACTIVE_STATES.includes(state);
  const controllable = ["running", "paused"].includes(state);
  if (current) current.state = state;

  $("setup-fields").disabled = live;
  $("busy-note").hidden = !live;
  $("preview").hidden = live;
  $("start").disabled = live;
  $("validate").disabled = live;
  $("dry").disabled = live;

  $("r-state").textContent = STATE_LABEL[state] || state;
  $("r-state").dataset.state = state;
  for (const id of ["pause", "stop", "open-event"]) $(id).hidden = !controllable;
  $("live-speed").closest("label").hidden = !controllable;
  if (!controllable) { closeEventPanel(); disarmStop(); }
  $("pause").textContent = state === "paused" ? "Resume" : "Pause";
  if (current) {
    $("sdk-figure").hidden = !!current.dry_run;
    $("r-name").textContent = current.scenario;
    const meta2 = [`${current.workers} worker${current.workers === 1 ? "" : "s"}`, current.run_id];
    if (current.dry_run) meta2.unshift("Practice run");
    $("r-meta").textContent = meta2.join(" · ");
    $("dl-summary").href = "/api/runs/current/export/summary.json";
    $("dl-csv").href = "/api/runs/current/export/timeseries.csv";
    syncSpeed(current.speed);
  }
}

function syncSpeed(speed) {
  const sel = $("live-speed");
  const v = String(speed);
  if (speed && ![...sel.options].some((o) => o.value === v)) {
    sel.add(new Option(`${v}×`, v));
  }
  if (speed) sel.value = v;
}

function disarmStop() {
  if (stopArmed) clearTimeout(stopArmed.timer);
  stopArmed = null;
  $("stop").textContent = "Stop";
}

/** Apply a control response only if it is for the run on screen and does not undo a finish reported over the WebSocket. */
function applyControl(res) {
  if (!current || res.run_id !== current.run_id || current.state === "finished") return false;
  current = { ...current, ...res };
  setRunState(current.state);
  return true;
}

function setMsg(id, text, kind = "") {
  $(id).textContent = text;
  $(id).dataset.kind = kind;
}
const msg = (text, kind) => setMsg("msg", text, kind);
const liveMsg = (text, kind) => setMsg("live-msg", text, kind);

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const res = await fetch(path, { headers: { "content-type": "application/json" }, ...opts });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = body.detail;
    throw new Error(typeof d === "string" ? d : Array.isArray(d) ? d.map((e) => `${(e.loc || []).join(".")}: ${e.msg}`).join("; ") : res.statusText);
  }
  return body;
}

// ------------------------------------------------------------------ setup form
function setChoice(name, value) {
  const sel = $(`${name}-choice`), inp = $(`p-${name}`);
  const v = value == null ? "" : String(value);
  const preset = [...sel.options].some((o) => o.value === v && v !== "custom");
  sel.value = preset ? v : "custom";
  inp.value = preset ? "" : v;
  inp.hidden = preset;
}
function getChoice(name) {
  const sel = $(`${name}-choice`);
  return sel.value === "custom" ? $(`p-${name}`).value.trim() : sel.value;
}

async function loadScenario(id) {
  const s = await api(`/api/scenarios/${encodeURIComponent(id)}`);
  yamlDirty = false;
  lastCheck = null;
  $("yaml").value = s.yaml;
  const r = s.raw;
  $("scenario-desc").textContent = r.description || "";
  $("p-concurrency").value = r.traffic?.base_concurrency ?? 1000;
  setChoice("speed", r.speed ?? 1);
  setChoice("duration", r.duration == null || r.duration === "" ? "0" : r.duration);
  setChoice("start", r.start_at ?? "now");
  $("p-seed").value = r.seed ?? 42;
  $("p-workers").value = r.workers ?? "";
  $("p-cap").value = r.events_per_sec_cap ?? "";
  $("p-sample").value = r.sample_payloads ? +(r.sample_payloads * 100).toFixed(3) : "";

  const groups = [];
  const tags = [];
  for (const [v, vc] of Object.entries(r.verticals || {})) {
    if ((vc && vc.enabled === false) || !meta.verticals[v]) continue;
    const mix = (vc && vc.platforms) || meta.default_platform_mix[v] || {};
    const plats = meta.verticals[v];
    const total = plats.reduce((a, p) => a + Number(mix[p] || 0), 0) || 1;
    tags.push(`<li>${escapeHtml(vertLabel(v))} <span class="hint">${plats.map(platLabel).join(", ")}</span></li>`);
    const e = escapeHtml;
    groups.push(`<div class="mix-group" data-v="${e(v)}"><h3>${e(vertLabel(v))}</h3>${plats.map((p) => {
      const id = `mix-${v}-${p}`;
      const val = Math.round((Number(mix[p] || 0) / total) * 100);
      return `<div class="mix-item"><label for="${e(id)}">${e(platLabel(p))}</label>
        <input id="${e(id)}" type="range" min="0" max="100" step="1" value="${val}" data-v="${e(v)}" data-p="${e(p)}">
        <output for="${e(id)}"></output></div>`;
    }).join("")}<p class="hint mix-error" hidden>Give at least one platform a share above 0.</p></div>`);
  }
  $("scenario-tags").innerHTML = tags.join("");
  $("mix-rows").innerHTML = groups.join("") || `<p class="hint">No apps are enabled in this scenario.</p>`;
  updateMix();
  updatePreview();
}

function updateMix() {
  for (const g of document.querySelectorAll(".mix-group")) {
    const inputs = [...g.querySelectorAll("input[type=range]")];
    const total = inputs.reduce((a, i) => a + Number(i.value), 0);
    for (const i of inputs) i.nextElementSibling.textContent = total ? `${Math.round((Number(i.value) / total) * 100)}%` : "0%";
    g.querySelector(".mix-error").hidden = total > 0;
  }
}

function formValues() {
  const num = (id) => ($(id).value === "" ? null : Number($(id).value));
  const speed = Number(getChoice("speed"));
  const duration = getChoice("duration");
  const sample = num("p-sample");
  return {
    concurrency: num("p-concurrency"),
    speed,
    duration: duration === "0" ? 0 : duration,
    start: getChoice("start"),
    seed: num("p-seed"),
    workers: num("p-workers"),
    cap: num("p-cap"),
    sample: sample == null ? null : sample / 100,
  };
}

function collectRequest() {
  const dry = $("dry").checked;
  // Once the YAML has been edited it is the source of truth: the form still shows the
  // scenario's original values, so sending them as overrides would undo the edits.
  if (yamlDirty) return { yaml: $("yaml").value, overrides: {}, dry_run: dry };
  const f = formValues();
  const o = {
    "traffic.base_concurrency": f.concurrency,
    speed: f.speed,
    duration: f.duration === "" ? null : f.duration,
    start_at: f.start || null,
    seed: f.seed,
    workers: f.workers,
    events_per_sec_cap: f.cap,
    sample_payloads: f.sample,
  };
  for (const inp of document.querySelectorAll("#mix-rows input[type=range]")) {
    o[`verticals.${inp.dataset.v}.platforms.${inp.dataset.p}`] = Number(inp.value) / 100;
  }
  for (const k of Object.keys(o)) if (o[k] === null || Number.isNaN(o[k])) delete o[k];
  return { scenario: $("scenario").value, overrides: o, dry_run: dry };
}

/** Client-side checks for the most common mistakes, before asking the server. */
function formProblem() {
  if (yamlDirty) return null;
  const f = formValues();
  if (f.concurrency == null || !(f.concurrency >= 0)) return "Enter how many simultaneous users to simulate.";
  if (!(f.speed > 0)) return "Enter a speed greater than 0.";
  if (f.duration !== 0 && !(parseDuration(f.duration) > 0)) return "Enter a simulated length such as 90m, 2h or 3d.";
  if (!f.start) return "Choose when event timestamps start.";
  if (f.start !== "now" && f.start.startsWith("-") && !(parseDuration(f.start.slice(1)) > 0)) return "Enter a start offset such as -24h.";
  for (const g of document.querySelectorAll(".mix-group .mix-error")) if (!g.hidden) return "Every app needs at least one platform with a share above 0.";
  if (f.sample != null && (f.sample < 0 || f.sample > 1)) return "Sample payloads must be between 0 and 100%.";
  return null;
}

function updatePreview() {
  const lines = [];
  if (yamlDirty) {
    lines.push("Using your edited YAML. The fields above are ignored.");
  } else {
    const f = formValues();
    const dur = f.duration === 0 ? Infinity : parseDuration(f.duration);
    const apps = [...document.querySelectorAll(".mix-group h3")].map((h) => h.textContent);
    if (f.concurrency != null) lines.push(`About <strong>${fmt(f.concurrency)}</strong> users at once${apps.length ? ` on ${escapeHtml(apps.join(", "))}` : ""}.`);
    if (f.speed > 0) {
      if (dur === Infinity) lines.push(f.speed === 1 ? "Runs in real time until you stop it." : `Simulates ${humanDuration(f.speed * 60)} every minute until you stop it.`);
      else if (Number.isFinite(dur)) lines.push(f.speed === 1 ? `Runs in real time for <strong>${humanDuration(dur)}</strong>.` : `Simulates ${humanDuration(dur)} in about <strong>${humanDuration(dur / f.speed)}</strong>.`);
      // Timestamps lead the wall clock when the simulated end lies in the future.
      const offset = f.start === "now" ? 0 : f.start.startsWith("-") ? -parseDuration(f.start.slice(1)) : NaN;
      const lead = Number.isFinite(dur) ? offset + dur - dur / f.speed : (f.speed > 1 && Number.isFinite(offset) ? Infinity : 0);
      if (Number.isFinite(offset) && lead > 60) {
        lines.push(`<span class="caution">Event timestamps will run ${lead === Infinity ? "further and further" : `up to ${humanDuration(lead)}`} ahead of the real clock. Choose an earlier start, such as “24 hours ago”, to keep them in the past.</span>`);
      }
    }
    if (lastCheck) lines.push(`Peak of about <strong>${fmt(lastCheck.peak)}</strong> users once patterns and events are applied.`);
  }
  lines.push($("dry").checked ? "Practice run: nothing is sent." : `Events are sent to <code>${escapeHtml(meta ? meta.destination.host : "")}</code>.`);
  $("preview").innerHTML = lines.map((l) => `<p>${l}</p>`).join("");
}

// ------------------------------------------------------------------ traffic events
function eventOptions() {
  const verts = (current && current.verticals) || Object.keys(meta.verticals);
  const entries = Object.entries(meta.event_types).filter(([, v]) => v && verts.includes(v));
  $("ev-type").innerHTML = entries.map(([k, v]) => `<option value="${escapeHtml(k)}">${escapeHtml(eventLabel(k))} · ${escapeHtml(vertLabel(v))}</option>`).join("");
  describeEvent();
}

function describeEvent() {
  const k = $("ev-type").value;
  const p = (meta.event_presets || {})[k] || {};
  const mag = Number(p.magnitude || 2);
  const hold = p.hold || "1h";
  $("ev-desc").textContent = EVENT_INFO[k] || "";
  $("ev-mag").innerHTML = [`<option value="">Default (×${mag})</option>`,
    ...[1.5, 2, 3, 5, 10].filter((m) => m !== mag).map((m) => `<option value="${m}">×${m} traffic</option>`)].join("");
  $("ev-hold").innerHTML = [`<option value="">Default (${escapeHtml(humanDuration(parseDuration(hold)))})</option>`,
    ...["15m", "30m", "1h", "2h", "6h", "24h"].filter((h) => h !== hold).map((h) => `<option value="${h}">${humanDuration(parseDuration(h))}</option>`)].join("");
}

function closeEventPanel() {
  $("event-panel").hidden = true;
  $("open-event").setAttribute("aria-expanded", "false");
}

// ------------------------------------------------------------------ websocket
function setConn(up, text) {
  $("conn").dataset.state = up ? "up" : "down";
  $("conn-text").textContent = text;
}

function connect() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws`);
  ws.onopen = () => setConn(true, "Live");
  ws.onclose = () => { setConn(false, "Reconnecting…"); setTimeout(connect, 1500); };
  ws.onmessage = (ev) => {
    const m = JSON.parse(ev.data);
    if (m.type === "hello") {
      resetCharts();
      if (m.status) {
        current = m.status;
        eventOptions();
        m.series.forEach((p) => pushPoint(p, false));
        Object.values(charts).forEach((c) => c.update("none"));
        if (m.series.length) renderPoint({ ...m.series[m.series.length - 1], funnels: m.status.funnels, state: m.status.state });
        else showLive();
        setRunState(m.status.state);
      }
    } else if (m.type === "started") {
      disarmStop();
      current = m.status;
      eventOptions();
      resetCharts();
      showLive();
      setRunState(m.status.state);
    } else if (m.type === "point") {
      if (current && m.run_id !== current.run_id) return;
      pushPoint(m);
      renderPoint(m);
    } else if (m.type === "finished") {
      if (current && m.run_id !== current.run_id) return;
      setRunState("finished");
      if (lastPoint) renderPoint({ ...lastPoint, state: "finished" });
      const s = m.summary || {};
      liveMsg(`Finished: ${fmt(s.total_events)} events, peak of ${fmt(s.peak_active_sessions)} users, ${fmt(s.avg_eps)} events per second on average.`, "ok");
      msg("");
      loadRuns();
    }
  };
}

// ------------------------------------------------------------------ previous runs
const FILE_LABEL = { "summary.json": "Summary", "timeseries.csv": "Time series (CSV)", "timeseries.jsonl": "Time series (JSONL)" };
const fileLabel = (f) => FILE_LABEL[f] || (f.match(/^payloads-w(\d+)\.jsonl$/) ? `Payload sample ${Number(f.match(/\d+/)[0]) + 1}` : f);

function runLabel(id) {
  const m = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/.exec(id);
  return m ? `${m[1]}-${m[2]}-${m[3]} ${m[4]}:${m[5]}` : id;
}

async function loadRuns() {
  const runs = (await api("/api/runs").catch(() => [])).filter((r) => r.files.length).slice(0, 10);
  $("history").hidden = !runs.length;
  $("history-empty").hidden = !!runs.length;
  $("past-runs").innerHTML = runs.map((r) => `<tr>
    <th scope="row">${escapeHtml(runLabel(r.id))} <span class="hint">${escapeHtml(r.id.split("-").pop())}</span></th>
    <td>${r.files.map((f) => `<a href="/api/runs/${encodeURIComponent(r.id)}/files/${encodeURIComponent(f)}" download>${escapeHtml(fileLabel(f))}</a>`).join("")}</td>
  </tr>`).join("");
}

// ------------------------------------------------------------------ wiring
async function main() {
  initCharts();
  meta = await api("/api/meta");
  $("dest").innerHTML = `Sending to <code>${escapeHtml(meta.destination.host)}</code>`;
  const list = await api("/api/scenarios");
  $("scenario").innerHTML = list.map((s) => `<option value="${escapeHtml(s.id)}">${escapeHtml(s.name || s.id)}</option>`).join("");
  eventOptions();
  if (list.length) await loadScenario(list[0].id);

  $("scenario").onchange = () => { msg(""); loadScenario($("scenario").value); };
  for (const name of ["speed", "duration", "start"]) {
    $(`${name}-choice`).addEventListener("change", () => {
      const custom = $(`${name}-choice`).value === "custom";
      $(`p-${name}`).hidden = !custom;
      if (custom) $(`p-${name}`).focus();
    });
  }
  $("setup").addEventListener("input", (e) => {
    if (e.target.id === "yaml") yamlDirty = true;
    if (e.target.type === "range") updateMix();
    lastCheck = null;
    updatePreview();
  });
  $("setup").addEventListener("change", () => updatePreview());
  $("setup").addEventListener("submit", (e) => e.preventDefault());
  $("yaml-reset").onclick = () => loadScenario($("scenario").value);

  $("validate").onclick = async () => {
    const problem = formProblem();
    if (problem) return msg(problem, "error");
    msg("Checking…");
    try {
      lastCheck = await api("/api/validate", { method: "POST", body: JSON.stringify(collectRequest()) });
      msg("Settings look good.", "ok");
      updatePreview();
    } catch (e) { msg(e.message, "error"); }
  };

  $("start").onclick = async () => {
    const problem = formProblem();
    if (problem) return msg(problem, "error");
    $("start").disabled = true;
    msg("Starting worker processes…");
    try {
      const res = await api("/api/runs", { method: "POST", body: JSON.stringify(collectRequest()) });
      // The WebSocket "started" message (and even later points) may already have arrived for this run.
      if (!current || current.run_id !== res.run_id) {
        current = res;
        eventOptions();
        resetCharts();
        showLive();
        setRunState(current.state);
      }
      liveMsg("");
      msg(`Started. Planned peak of about ${fmt(res.planned_peak)} users.`, "ok");
      $("main").focus({ preventScroll: true });
    } catch (e) { msg(e.message, "error"); $("start").disabled = false; }
  };

  $("pause").onclick = async () => {
    const resume = current && current.state === "paused";
    try { if (applyControl(await api(`/api/runs/current/${resume ? "resume" : "pause"}`, { method: "POST" }))) liveMsg(""); }
    catch (e) { liveMsg(e.message, "error"); }
  };

  $("stop").onclick = async () => {
    const runId = current && current.run_id;
    if (!stopArmed || stopArmed.runId !== runId) {
      disarmStop();
      $("stop").textContent = "Click again to stop";
      stopArmed = { runId, timer: setTimeout(disarmStop, 4000) };
      return;
    }
    disarmStop();
    liveMsg("Stopping. Sending what's queued, then saving exports…");
    try { applyControl(await api("/api/runs/current/stop", { method: "POST" })); }
    catch (e) { liveMsg(e.message, "error"); }
  };

  $("live-speed").onchange = async () => {
    const speed = Number($("live-speed").value);
    try { if (applyControl(await api("/api/runs/current/speed", { method: "POST", body: JSON.stringify({ speed }) }))) liveMsg(`Speed changed to ${speed}×.`, "ok"); }
    catch (e) { liveMsg(e.message, "error"); }
  };

  $("open-event").onclick = () => {
    const open = $("event-panel").hidden;
    $("event-panel").hidden = !open;
    $("open-event").setAttribute("aria-expanded", String(open));
    if (open) $("ev-type").focus();
  };
  $("ev-type").onchange = describeEvent;
  $("event-panel").addEventListener("submit", async (e) => {
    e.preventDefault();
    const spec = { type: $("ev-type").value };
    if ($("ev-mag").value) spec.magnitude = Number($("ev-mag").value);
    if ($("ev-hold").value) spec.hold = $("ev-hold").value;
    try {
      const ov = await api("/api/runs/current/events", { method: "POST", body: JSON.stringify(spec) });
      liveMsg(`${eventLabel(ov.name)} started: ×${ov.magnitude} traffic on ${vertLabel(ov.vertical)}.`, "ok");
      closeEventPanel();
    } catch (err) { liveMsg(err.message, "error"); }
  });

  connect();
  loadRuns();
}

main().catch((e) => msg(`The dashboard could not load: ${e.message}`, "error"));
