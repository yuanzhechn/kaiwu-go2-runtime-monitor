"use strict";

const $ = (id) => document.getElementById(id);
const MAX_POINTS = 3000;
let revision = 0;
let currentCsvPath = "";
let latestSample = null;
let latestUwbStatus = null;

// 让左侧监控面板形成自然纵向流，避免较高的 UWB 雷达把速度面板拉出大块空白。
const heroGrid = document.querySelector(".hero-grid");
const speedPanel = document.querySelector(".speed-panel");
const detailGrid = document.querySelector(".detail-grid");
if (heroGrid && speedPanel && detailGrid) {
  const leftStack = document.createElement("div");
  leftStack.className = "left-stack";
  heroGrid.insertBefore(leftStack, speedPanel);
  leftStack.append(speedPanel, detailGrid);
}

function finite(value) { return typeof value === "number" && Number.isFinite(value); }
function value(value, digits = 3) { return finite(value) ? value.toFixed(digits) : "—"; }
function percent(value) { return finite(value) ? (value * 100).toFixed(1) : "—"; }
function sourceName(code) { return ({0: "固定速度", 1: "UWB", 2: "Nav/零速"})[code] || "未知"; }
function feedbackName(name) { return ({sport: "SportState 实测", uwb_estimate: "UWB 估算", none: "无反馈"})[name] || "未知"; }

class LineChart {
  constructor(canvas, series, min = null, max = null, legend = null) {
    this.canvas = canvas;
    this.series = series;
    this.min = min;
    this.max = max;
    this.points = [];
    this.legend = legend;
    this.activeSeries = null;
    this.hoverIndex = null;
    this.layout = null;
    this.resize = this.draw.bind(this);
    window.addEventListener("resize", this.resize);
    canvas.addEventListener("mousemove", event => this.onMove(event));
    canvas.addEventListener("mouseleave", () => { this.activeSeries = null; this.hoverIndex = null; this.syncLegend(); this.draw(); });
    legend?.querySelectorAll("[data-series]").forEach(item => {
      item.addEventListener("mouseenter", () => { this.activeSeries = Number(item.dataset.series); this.syncLegend(); this.draw(); });
      item.addEventListener("mouseleave", () => { this.activeSeries = null; this.syncLegend(); this.draw(); });
    });
  }
  set(points) { this.points = points.slice(-MAX_POINTS); this.draw(); }
  append(points) { this.points.push(...points); if (this.points.length > MAX_POINTS) this.points.splice(0, this.points.length - MAX_POINTS); this.draw(); }
  syncLegend() {
    this.legend?.querySelectorAll("[data-series]").forEach(item => {
      const index = Number(item.dataset.series);
      item.classList.toggle("active", this.activeSeries === index);
      item.classList.toggle("faded", this.activeSeries !== null && this.activeSeries !== index);
    });
  }
  onMove(event) {
    if (!this.layout || !this.points.length) return;
    const rect = this.canvas.getBoundingClientRect();
    const px = event.clientX - rect.left, py = event.clientY - rect.top;
    const {pad, plotW, plotH, yFor} = this.layout;
    if (px < pad.l || px > pad.l + plotW || py < pad.t || py > pad.t + plotH) {
      this.activeSeries = null; this.hoverIndex = null; this.syncLegend(); this.draw(); return;
    }
    const ratio = (px - pad.l) / plotW;
    this.hoverIndex = Math.max(0, Math.min(this.points.length - 1, Math.round(ratio * (this.points.length - 1))));
    const point = this.points[this.hoverIndex];
    let best = null, bestDistance = Infinity;
    this.series.forEach((series, index) => {
      const v = series.get(point);
      if (!finite(v)) return;
      const distance = Math.abs(py - yFor(v));
      if (distance < bestDistance) { bestDistance = distance; best = index; }
    });
    this.activeSeries = bestDistance <= 15 ? best : null;
    this.canvas.style.cursor = this.activeSeries === null ? "crosshair" : "pointer";
    this.syncLegend(); this.draw();
  }
  draw() {
    const rect = this.canvas.getBoundingClientRect();
    if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1;
    const height = Number(this.canvas.getAttribute("height")) || 160;
    this.canvas.width = Math.round(rect.width * dpr);
    this.canvas.height = Math.round(height * dpr);
    const ctx = this.canvas.getContext("2d");
    ctx.scale(dpr, dpr);
    const w = rect.width, h = height, pad = {l: 38, r: 10, t: 10, b: 24};
    const plotW = w - pad.l - pad.r, plotH = h - pad.t - pad.b;
    ctx.clearRect(0, 0, w, h);
    const values = [];
    this.points.forEach(p => this.series.forEach(s => { const v = s.get(p); if (finite(v)) values.push(v); }));
    let lo = this.min !== null ? this.min : Math.min(0, ...values);
    let hi = this.max !== null ? this.max : Math.max(0, ...values);
    if (!values.length) { lo = -1; hi = 1; }
    if (Math.abs(hi - lo) < .001) { hi += .1; lo -= .1; }
    const range = hi - lo;
    ctx.font = "10px sans-serif"; ctx.fillStyle = "#6f8981"; ctx.strokeStyle = "rgba(169,213,197,.10)"; ctx.lineWidth = 1;
    for (let i = 0; i <= 4; i++) {
      const y = pad.t + (h - pad.t - pad.b) * i / 4;
      ctx.beginPath(); ctx.moveTo(pad.l, y); ctx.lineTo(w - pad.r, y); ctx.stroke();
      ctx.fillText((hi - range * i / 4).toFixed(2), 2, y + 3);
    }
    if (!this.points.length) return;
    const x = i => pad.l + plotW * (this.points.length === 1 ? 1 : i / (this.points.length - 1));
    const y = v => pad.t + (hi - v) / range * plotH;
    this.layout = {pad, plotW, plotH, yFor: y};
    const order = this.series.map((_, index) => index);
    if (this.activeSeries !== null) { order.splice(order.indexOf(this.activeSeries), 1); order.push(this.activeSeries); }
    order.forEach(index => {
      const s = this.series[index];
      ctx.save();
      ctx.globalAlpha = this.activeSeries === null || this.activeSeries === index ? 1 : .14;
      ctx.beginPath(); ctx.strokeStyle = s.color; ctx.lineWidth = this.activeSeries === index ? 3.4 : 1.8; let started = false;
      this.points.forEach((p, i) => { const v = s.get(p); if (!finite(v)) return; if (!started) { ctx.moveTo(x(i), y(v)); started = true; } else ctx.lineTo(x(i), y(v)); });
      ctx.stroke(); ctx.restore();
    });
    ctx.fillStyle = "#6f8981"; ctx.fillText(`最近 ${this.points.length} 帧`, pad.l, h - 6);
    if (this.hoverIndex !== null) {
      const point = this.points[this.hoverIndex], hx = x(this.hoverIndex);
      ctx.save(); ctx.strokeStyle = "rgba(255,255,255,.22)"; ctx.setLineDash([3, 4]);
      ctx.beginPath(); ctx.moveTo(hx, pad.t); ctx.lineTo(hx, pad.t + plotH); ctx.stroke(); ctx.setLineDash([]);
      const entries = this.series.map((s, index) => ({...s, index, value: s.get(point)})).filter(item => finite(item.value));
      entries.forEach(item => { ctx.fillStyle = item.color; ctx.globalAlpha = this.activeSeries === null || this.activeSeries === item.index ? 1 : .28; ctx.beginPath(); ctx.arc(hx, y(item.value), item.index === this.activeSeries ? 4.5 : 3, 0, Math.PI * 2); ctx.fill(); });
      ctx.globalAlpha = 1;
      const boxW = 158, boxH = 24 + entries.length * 17;
      const boxX = hx + boxW + 12 > w ? hx - boxW - 10 : hx + 10;
      const boxY = Math.max(6, Math.min(h - boxH - 6, pad.t + 8));
      ctx.fillStyle = "rgba(4,12,10,.94)"; ctx.strokeStyle = "rgba(169,213,197,.25)"; ctx.lineWidth = 1;
      ctx.beginPath(); ctx.roundRect(boxX, boxY, boxW, boxH, 8); ctx.fill(); ctx.stroke();
      ctx.font = "600 10px sans-serif"; ctx.fillStyle = "#89a29a"; ctx.fillText(`frame ${point.frame ?? "—"}`, boxX + 10, boxY + 16);
      entries.forEach((item, row) => {
        ctx.globalAlpha = this.activeSeries === null || this.activeSeries === item.index ? 1 : .35;
        ctx.fillStyle = item.color; ctx.fillRect(boxX + 10, boxY + 27 + row * 17, 9, 2);
        ctx.fillStyle = "#e9f4f0"; ctx.font = "11px sans-serif"; ctx.fillText(`${item.label}: ${value(item.value)}`, boxX + 25, boxY + 31 + row * 17);
      });
      ctx.restore();
    }
  }
}

class UwbRadar {
  constructor(canvas) { this.canvas = canvas; this.sample = null; this.status = null; this.history = []; window.addEventListener("resize", () => this.draw()); }
  setHistory(history) { this.history = history.slice(-120); this.draw(); }
  append(samples) { this.history.push(...samples); if (this.history.length > 120) this.history.splice(0, this.history.length - 120); this.draw(); }
  update(sample, status) { this.sample = sample; this.status = status; this.draw(); }
  draw() {
    const rect = this.canvas.getBoundingClientRect(); if (!rect.width) return;
    const dpr = window.devicePixelRatio || 1, w = rect.width, h = Number(this.canvas.getAttribute("height")) || 270;
    this.canvas.width = Math.round(w * dpr); this.canvas.height = Math.round(h * dpr);
    const ctx = this.canvas.getContext("2d"); ctx.scale(dpr, dpr); ctx.clearRect(0, 0, w, h);
    const cx = w / 2, cy = h * .77, radius = Math.min(w * .42, h * .62);
    const distances = this.history.map(p => p.uwb?.distance).filter(finite);
    const currentDistance = this.sample?.uwb?.distance;
    const scaleDistance = Math.max(3, finite(currentDistance) ? currentDistance * 1.18 : 0, ...distances.slice(-60).map(v => v * 1.05));
    ctx.save();
    ctx.fillStyle = "rgba(75,203,211,.045)"; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.arc(cx, cy, radius, -Math.PI * .82, -Math.PI * .18); ctx.closePath(); ctx.fill();
    ctx.strokeStyle = "rgba(169,213,197,.13)"; ctx.lineWidth = 1; ctx.setLineDash([3, 5]);
    for (let ring = 1; ring <= 3; ring++) { const r = radius * ring / 3; ctx.beginPath(); ctx.arc(cx, cy, r, Math.PI, 2 * Math.PI); ctx.stroke(); ctx.fillStyle = "#668078"; ctx.font = "9px sans-serif"; ctx.fillText(`${(scaleDistance * ring / 3).toFixed(1)}m`, cx + 4, cy - r + 11); }
    ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(cx, cy - radius); ctx.stroke(); ctx.setLineDash([]);
    const toPoint = p => {
      const distance = p.uwb?.distance, beta = p.uwb?.beta;
      if (!finite(distance) || !finite(beta)) return null;
      const r = Math.min(radius, distance / scaleDistance * radius);
      return {x: cx + Math.sin(beta) * r, y: cy - Math.cos(beta) * r};
    };
    const trail = this.history.slice(-80).filter(p => p.uwb?.valid).map(toPoint).filter(Boolean);
    if (trail.length > 1) { ctx.beginPath(); trail.forEach((p, i) => i ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)); ctx.strokeStyle = "rgba(75,203,211,.42)"; ctx.lineWidth = 1.4; ctx.stroke(); }
    const point = this.sample ? toPoint(this.sample) : null;
    const state = this.status?.state || "unknown";
    const targetColor = ({good: "#54e39a", warn: "#f1b953", bad: "#ff6b6b", unknown: "#89a29a"})[state];
    if (point) {
      ctx.strokeStyle = targetColor; ctx.globalAlpha = .45; ctx.beginPath(); ctx.moveTo(cx, cy); ctx.lineTo(point.x, point.y); ctx.stroke(); ctx.globalAlpha = 1;
      ctx.fillStyle = targetColor; ctx.shadowColor = targetColor; ctx.shadowBlur = 14; ctx.beginPath(); ctx.arc(point.x, point.y, 7, 0, Math.PI * 2); ctx.fill(); ctx.shadowBlur = 0;
      ctx.fillStyle = "#dcebe6"; ctx.font = "10px sans-serif"; ctx.fillText("UWB 目标", point.x + 10, point.y - 8);
    }
    ctx.translate(cx, cy); ctx.fillStyle = "#dcebe6"; ctx.beginPath(); ctx.moveTo(0, -15); ctx.lineTo(10, 12); ctx.lineTo(0, 8); ctx.lineTo(-10, 12); ctx.closePath(); ctx.fill();
    ctx.strokeStyle = "#4bcbd3"; ctx.lineWidth = 2.5; const command = this.sample?.command || {}; const vx = finite(command.vx) ? command.vx : 0, vy = finite(command.vy) ? command.vy : 0; const mag = Math.hypot(vx, vy);
    if (mag > .005) { const arrowScale = Math.min(58, 22 + mag * 110); const ax = vy / mag * arrowScale, ay = -vx / mag * arrowScale; ctx.beginPath(); ctx.moveTo(0, 0); ctx.lineTo(ax, ay); ctx.stroke(); ctx.fillStyle = "#4bcbd3"; ctx.beginPath(); ctx.arc(ax, ay, 3, 0, Math.PI * 2); ctx.fill(); }
    ctx.restore();
    if (!point) { ctx.fillStyle = "rgba(255,107,107,.88)"; ctx.font = "600 13px sans-serif"; ctx.textAlign = "center"; ctx.fillText("当前没有可绘制的 UWB 目标", cx, h * .40); ctx.textAlign = "left"; }
  }
}

const speedChart = new LineChart($("speed-chart"), [
  {label: "理论 vx", color: "#f1b953", get: p => p.command?.theory_vx},
  {label: "发布 vx", color: "#4bcbd3", get: p => p.command?.vx},
  {label: "实际/估算 vx", color: "#54e39a", get: p => p.feedback?.vx},
], null, null, $("speed-legend"));
const uwbChart = new LineChart($("uwb-chart"), [
  {label: "目标距离", color: "#4bcbd3", get: p => p.uwb?.distance},
  {label: "数据年龄", color: "#f1b953", get: p => p.uwb?.age_s},
], 0, null, $("uwb-legend"));
const depthChart = new LineChart($("depth-chart"), [
  {label: "全图无效率", color: "#ff6b6b", get: p => p.depth?.invalid},
  {label: "正前无效率", color: "#f1b953", get: p => p.depth?.front_invalid},
], 0, 1, $("depth-legend"));
const performanceChart = new LineChart($("performance-chart"), [
  {label: "推理耗时", color: "#54e39a", get: p => p.performance?.inference_ms},
  {label: "循环耗时", color: "#4bcbd3", get: p => p.performance?.loop_ms},
], 0, null, $("performance-legend"));
const uwbRadar = new UwbRadar($("uwb-radar"));

function renderCard(name, data) {
  const card = document.querySelector(`[data-status="${name}"]`);
  if (!card || !data) return;
  card.className = `status-card ${data.state || "unknown"}`;
  card.querySelector("strong").textContent = data.label || "未知";
  card.querySelector("p").textContent = data.detail || "—";
  card.title = data.detail || "";
}

function renderStatus(status) {
  ["controller", "vision", "uwb", "camera", "feedback", "inference"].forEach(name => renderCard(name, status[name]));
  latestUwbStatus = status.uwb || null;
  const csv = status.csv || {};
  currentCsvPath = csv.path || "";
  $("csv-path").textContent = currentCsvPath || "尚未找到";
  $("csv-age").textContent = finite(csv.age_s) ? `${csv.age_s.toFixed(2)} s` : "—";
  updateUwbHealth(latestSample, latestUwbStatus);
  uwbRadar.update(latestSample, latestUwbStatus);
}

function renderLatest(sample) {
  if (!sample) return;
  latestSample = sample;
  const c = sample.command || {}, u = sample.uwb || {}, f = sample.feedback || {}, d = sample.depth || {}, p = sample.performance || {};
  $("theory-vx").textContent = value(c.theory_vx);
  $("command-vx").textContent = value(c.vx);
  $("actual-vx").textContent = value(f.vx);
  $("error-vx").textContent = value(f.err_vx);
  $("command-wz").textContent = value(c.wz);
  $("actual-wz").textContent = value(f.wz);
  $("feedback-source").textContent = feedbackName(f.source);
  $("actual-source").textContent = feedbackName(f.source);
  $("command-source").textContent = sourceName(c.source);
  $("uwb-valid").textContent = u.valid ? "有效" : "无效";
  $("uwb-distance").textContent = value(u.distance);
  $("uwb-beta").textContent = value(u.beta);
  $("uwb-age").textContent = value(u.age_s);
  $("uwb-closing").textContent = value(u.closing);
  $("uwb-error").textContent = u.error ?? "—";
  $("uwb-channel").textContent = u.channel ?? "—";
  const betaDeg = finite(u.beta) ? u.beta * 180 / Math.PI : null;
  let direction = "等待 UWB";
  if (finite(betaDeg)) {
    if (Math.abs(betaDeg) < 8) direction = `目标基本在正前 · ${betaDeg.toFixed(1)}°`;
    else direction = `目标偏${betaDeg > 0 ? "左" : "右"} · ${Math.abs(betaDeg).toFixed(1)}°`;
  }
  $("uwb-direction").textContent = direction;
  $("depth-invalid").textContent = percent(d.invalid);
  $("front-invalid").textContent = percent(d.front_invalid);
  $("front-min").textContent = value(d.front_min);
  $("depth-same").textContent = d.same_frames ?? "—";
  $("inference-ms").textContent = value(p.inference_ms, 2);
  $("loop-ms").textContent = value(p.loop_ms, 2);
  $("deadline-misses").textContent = p.deadline_misses ?? "—";
  $("consecutive-errors").textContent = p.consecutive_errors ?? "—";
  $("frame-id").textContent = `frame ${sample.frame ?? "—"}`;
  updateUwbHealth(sample, latestUwbStatus);
  uwbRadar.update(sample, latestUwbStatus);
}

function setCheck(id, state, text) {
  const item = $(id); item.className = `check ${state}`; item.textContent = text;
}

function updateUwbHealth(sample, status) {
  const u = sample?.uwb;
  if (!u) {
    setCheck("uwb-health-fresh", "unknown", "数据新鲜度未知");
    setCheck("uwb-health-enabled", "unknown", "使能状态未知");
    setCheck("uwb-health-measure", "unknown", "测距状态未知");
    return;
  }
  const fresh = u.valid && finite(u.age_s) && u.age_s <= .5;
  setCheck("uwb-health-fresh", fresh ? "good" : (finite(u.age_s) ? "bad" : "unknown"), fresh ? `数据新鲜 ${u.age_s.toFixed(3)}s` : `数据过期 ${value(u.age_s)}s`);
  const enabled = u.enabled && u.error === 0;
  setCheck("uwb-health-enabled", enabled ? "good" : (u.enabled ? "warn" : "bad"), enabled ? "跟随已使能 · 无错误" : `使能=${u.enabled ? 1 : 0} · 错误=${u.error}`);
  const measured = finite(u.distance) && finite(u.beta);
  setCheck("uwb-health-measure", measured && u.valid ? "good" : (measured ? "warn" : "bad"), measured ? `测距 ${u.distance.toFixed(2)}m` : "没有有效距离/方向");
  if (status?.state === "bad" && fresh) setCheck("uwb-health-fresh", "warn", "CSV 新鲜，但 UWB 状态异常");
}

function appendSamples(samples) {
  if (!samples?.length) return;
  speedChart.append(samples); uwbChart.append(samples); depthChart.append(samples); performanceChart.append(samples);
  uwbRadar.append(samples);
}

function streamState(kind, label) {
  $("stream-dot").className = `stream-dot ${kind}`;
  $("stream-label").textContent = label;
}

async function initialLoad() {
  const response = await fetch("/api/status", {cache: "no-store"});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const data = await response.json();
  revision = data.revision || 0;
  renderStatus(data.status || {}); renderLatest(data.latest);
  const history = data.history || [];
  speedChart.set(history); uwbChart.set(history); depthChart.set(history); performanceChart.set(history);
  uwbRadar.setHistory(history);
}

let events;
function connectEvents() {
  if (events) events.close();
  events = new EventSource(`/api/events?after=${revision}`);
  events.onopen = () => streamState("online", "实时流已连接");
  events.onmessage = event => {
    const data = JSON.parse(event.data);
    revision = data.revision || revision;
    renderStatus(data.status || {}); renderLatest(data.latest); appendSamples(data.samples || []);
    $("last-update").textContent = new Date().toLocaleTimeString();
  };
  events.onerror = () => streamState("offline", "连接中断，自动重连");
}

async function refreshLogs() {
  try {
    const response = await fetch("/api/logs?limit=120", {cache: "no-store"});
    const data = await response.json();
    $("log-path").textContent = data.path || data.message || "未找到文本日志";
    $("log-output").textContent = data.lines?.length ? data.lines.join("\n") : (data.message || "暂无日志内容");
    $("log-output").scrollTop = $("log-output").scrollHeight;
  } catch (error) { $("log-output").textContent = `日志读取失败：${error.message}`; }
}

$("refresh-log").addEventListener("click", refreshLogs);
$("copy-path").addEventListener("click", async () => {
  if (!currentCsvPath) return;
  try {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(currentCsvPath);
    else throw new Error("clipboard API unavailable");
  } catch (_) {
    const area = document.createElement("textarea");
    area.value = currentCsvPath; document.body.appendChild(area); area.select();
    document.execCommand("copy"); area.remove();
  }
  $("copy-path").textContent = "已复制";
  setTimeout(() => $("copy-path").textContent = "复制 CSV 路径", 1200);
});

(async () => {
  try { await initialLoad(); connectEvents(); streamState("online", "实时流已连接"); }
  catch (error) { streamState("offline", `连接失败：${error.message}`); connectEvents(); }
  refreshLogs();
  setInterval(refreshLogs, 5000);
})();
