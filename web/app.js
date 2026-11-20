"use strict";
/* ---------- utilities ---------- */
const $ = (s, r) => (r || document).querySelector(s);
const el = (tag, cls, text) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
};
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const fmtTs = (t) => {
  const d = new Date(t * 1000);
  const p = (v) => String(v).padStart(2, "0");
  return `${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fmtDay = (t) => {
  const d = new Date(t * 1000);
  const p = (v) => String(v).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
};
const fmtHours = (h) => (h == null ? "—" : h >= 72 ? (h / 24).toFixed(1) + " 天" : h.toFixed(0) + " h");

async function api(path, opts) {
  const r = await fetch(path, opts);
  if (!r.ok) throw new Error(`${r.status} ${await r.text()}`);
  return r.json();
}

const SERIES = [css("--series-1"), css("--series-2"), css("--series-3")];
const SEV = {
  crit: { color: css("--status-critical"), label: "严重", icon: "●" },
  warn: { color: css("--status-serious"), label: "警告", icon: "▲" },
  info: { color: css("--muted"), label: "提示", icon: "○" },
};
const QUALITY_LABEL = { ok: "正常", dup_resolved: "重复采样", gap_filled: "缺测插补", drift_corrected: "漂移修正" };
const QUALITY_COLOR = { ok: css("--series-1"), dup_resolved: css("--series-2"), gap_filled: css("--series-3"), drift_corrected: css("--series-4") };
const RULE_LABEL = {
  metric_z_crit: "指标越限(临界)", metric_z_warn: "指标越限(预警)",
  score_crit: "健康分临界", score_warn: "健康分预警",
  sustained_degradation: "持续劣化", low_coverage: "数据缺失",
};
const DISP_LABEL = { fixed: "已修复", false_positive: "误报", monitoring: "持续观察", parts_ordered: "已订备件" };
const STATUS_LABEL = { open: "未处理", ack: "已确认", resolved: "已解决" };

/* ---------- tooltip ---------- */
const tip = $("#tooltip");
function showTip(x, y, node) {
  tip.replaceChildren(node);
  tip.hidden = false;
  const r = tip.getBoundingClientRect();
  tip.style.left = Math.min(x + 14, innerWidth - r.width - 8) + "px";
  tip.style.top = Math.max(8, y - r.height - 10) + "px";
}
function hideTip() { tip.hidden = true; }
function tipRow(keyNode, label, value) {
  const row = el("div", "t-row");
  row.append(keyNode, el("span", "sec", label), el("span", "v", value));
  return row;
}

/* ---------- SVG helpers ---------- */
const SVGNS = "http://www.w3.org/2000/svg";
function svg(tag, attrs) {
  const n = document.createElementNS(SVGNS, tag);
  for (const k in attrs) n.setAttribute(k, attrs[k]);
  return n;
}
const lerp = (d0, d1, r0, r1) => (v) => r0 + ((v - d0) / (d1 - d0 || 1)) * (r1 - r0);

function axes(g, x, y, xDomain, yDomain, W, H, pad, xFmt, yFmt) {
  const gy = svg("g", {});
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const v = yDomain[0] + ((yDomain[1] - yDomain[0]) * i) / ticks;
    const yy = y(v);
    gy.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: yy, y2: yy, stroke: css("--gridline"), "stroke-width": 1 }));
    const t = svg("text", { x: pad.l - 6, y: yy + 4, "text-anchor": "end", "font-size": 11, fill: css("--muted") });
    t.textContent = yFmt ? yFmt(v) : v;
    gy.append(t);
  }
  const xt = 6;
  for (let i = 0; i <= xt; i++) {
    const v = xDomain[0] + ((xDomain[1] - xDomain[0]) * i) / xt;
    const t = svg("text", { x: x(v), y: H - 6, "text-anchor": "middle", "font-size": 11, fill: css("--muted") });
    t.textContent = xFmt(v);
    gy.append(t);
  }
  g.append(gy);
}

/* line/area chart with bands, vlines, markers, crosshair */
function lineChart(cfg) {
  const W = Math.max(320, cfg.el.clientWidth || 900);
  const H = cfg.height || 220;
  const pad = { l: 44, r: 12, t: 14, b: 24 };
  const x = lerp(cfg.xDomain[0], cfg.xDomain[1], pad.l, W - pad.r);
  const y = lerp(cfg.yDomain[0], cfg.yDomain[1], H - pad.b, pad.t);
  const root = svg("svg", { width: W, height: H, role: "img" });

  (cfg.bands || []).forEach((b) => {
    root.append(svg("rect", { x: x(b.x0), y: pad.t, width: Math.max(1, x(b.x1) - x(b.x0)), height: H - pad.b - pad.t, fill: css("--muted"), opacity: 0.18 }));
  });
  axes(root, x, y, cfg.xDomain, cfg.yDomain, W, H, pad, cfg.xFmt || fmtTs, cfg.yTickFmt);

  (cfg.vlines || []).forEach((v) => {
    root.append(svg("line", { x1: x(v.x), x2: x(v.x), y1: pad.t, y2: H - pad.b, stroke: v.color, "stroke-width": 1.5 }));
    const t = svg("text", { x: x(v.x) + 4, y: pad.t + 10, "font-size": 11, fill: css("--text-secondary") });
    t.textContent = v.label || "";
    root.append(t);
  });

  cfg.series.forEach((s) => {
    const runs = [];
    let cur = [];
    s.points.forEach(([px, py]) => {
      if (py == null) { if (cur.length) runs.push(cur); cur = []; }
      else cur.push([px, py]);
    });
    if (cur.length) runs.push(cur);
    runs.forEach((run) => {
      if (s.area && run.length > 1) {
        const d = run.map((p, i) => `${i ? "L" : "M"}${x(p[0])},${y(p[1])}`).join("") +
          `L${x(run[run.length - 1][0])},${y(cfg.yDomain[0])}L${x(run[0][0])},${y(cfg.yDomain[0])}Z`;
        root.append(svg("path", { d, fill: s.color, opacity: 0.1 }));
      }
      if (run.length === 1) {
        root.append(svg("circle", { cx: x(run[0][0]), cy: y(run[0][1]), r: 4, fill: s.color, stroke: css("--surface-1"), "stroke-width": 2 }));
      } else {
        const d = run.map((p, i) => `${i ? "L" : "M"}${x(p[0])},${y(p[1])}`).join("");
        root.append(svg("path", { d, fill: "none", stroke: s.color, "stroke-width": 2, "stroke-linejoin": "round", "stroke-linecap": "round" }));
      }
    });
    const last = [...s.points].reverse().find((p) => p[1] != null);
    if (last) root.append(svg("circle", { cx: x(last[0]), cy: y(last[1]), r: 4, fill: s.color, stroke: css("--surface-1"), "stroke-width": 2 }));
  });

  (cfg.markers || []).forEach((m) => {
    const mx = x(m.x), my = m.y != null ? y(m.y) : pad.t + 4;
    let node;
    if (m.shape === "tri") {
      node = svg("path", { d: `M${mx},${my - 5}L${mx + 5},${my + 4}L${mx - 5},${my + 4}Z`, fill: m.color, stroke: css("--surface-1"), "stroke-width": 2 });
    } else if (m.shape === "diamond") {
      node = svg("path", { d: `M${mx},${my - 5}L${mx + 5},${my}L${mx},${my + 5}L${mx - 5},${my}Z`, fill: m.color, stroke: css("--surface-1"), "stroke-width": 2 });
    } else if (m.shape === "down") {
      node = svg("path", { d: `M${mx},${my + 5}L${mx + 5},${my - 4}L${mx - 5},${my - 4}Z`, fill: m.color, stroke: css("--surface-1"), "stroke-width": 2 });
    } else {
      node = svg("circle", { cx: mx, cy: my, r: 4.5, fill: m.color, stroke: css("--surface-1"), "stroke-width": 2 });
    }
    node.style.cursor = "pointer";
    const hit = svg("circle", { cx: mx, cy: my, r: 12, fill: "transparent" });
    hit.style.cursor = "pointer";
    const show = (ev) => {
      const n = el("div");
      n.append(el("div", "t-title", m.title || ""));
      if (m.sub) n.append(el("div", "sec", m.sub));
      showTip(ev.clientX, ev.clientY, n);
    };
    hit.addEventListener("pointermove", show);
    hit.addEventListener("pointerleave", hideTip);
    hit.addEventListener("click", () => cfg.onMarkerClick && cfg.onMarkerClick(m));
    node.addEventListener("click", () => cfg.onMarkerClick && cfg.onMarkerClick(m));
    root.append(node, hit);
  });

  // crosshair
  const cross = svg("line", { y1: pad.t, y2: H - pad.b, stroke: css("--muted"), "stroke-width": 1, visibility: "hidden" });
  root.append(cross);
  const hitRect = svg("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.b - pad.t, fill: "transparent" });
  root.append(hitRect);
  hitRect.addEventListener("pointermove", (ev) => {
    const rect = root.getBoundingClientRect();
    const mxv = cfg.xDomain[0] + ((ev.clientX - rect.left - pad.l) / (rect.width - pad.l - pad.r)) * (cfg.xDomain[1] - cfg.xDomain[0]);
    let best = null;
    cfg.series.forEach((s) => {
      s.points.forEach((p) => {
        if (p[1] == null) return;
        const d = Math.abs(p[0] - mxv);
        if (!best || d < best.d) best = { d, x: p[0] };
      });
    });
    if (!best) return;
    cross.setAttribute("x1", x(best.x)); cross.setAttribute("x2", x(best.x));
    cross.setAttribute("visibility", "visible");
    const n = el("div");
    n.append(el("div", "t-title", (cfg.xFmt || fmtTs)(best.x)));
    cfg.series.forEach((s) => {
      const p = s.points.find((q) => q[0] === best.x && q[1] != null);
      if (!p) return;
      const key = el("span", "line-key"); key.style.background = s.color;
      n.append(tipRow(key, s.name, cfg.yTipFmt ? cfg.yTipFmt(p[1]) : p[1]));
    });
    showTip(ev.clientX, ev.clientY, n);
  });
  hitRect.addEventListener("pointerleave", () => { cross.setAttribute("visibility", "hidden"); hideTip(); });

  cfg.el.replaceChildren(root);
}

/* scatter with h-bands / h-lines / per-point hover */
function scatterChart(cfg) {
  const W = Math.max(320, cfg.el.clientWidth || 700);
  const H = cfg.height || 240;
  const pad = { l: 48, r: 12, t: 14, b: 24 };
  const x = lerp(cfg.xDomain[0], cfg.xDomain[1], pad.l, W - pad.r);
  const y = lerp(cfg.yDomain[0], cfg.yDomain[1], H - pad.b, pad.t);
  const root = svg("svg", { width: W, height: H, role: "img" });
  (cfg.vBands || []).forEach((b) => {
    root.append(svg("rect", { x: x(b.x0), y: pad.t, width: Math.max(1, x(b.x1) - x(b.x0)), height: H - pad.b - pad.t, fill: css("--status-serious"), opacity: 0.08 }));
  });
  (cfg.hBands || []).forEach((b) => {
    root.append(svg("rect", { x: pad.l, y: y(b.y1), width: W - pad.l - pad.r, height: y(b.y0) - y(b.y1), fill: css("--muted"), opacity: 0.15 }));
  });
  axes(root, x, y, cfg.xDomain, cfg.yDomain, W, H, pad, cfg.xFmt || fmtTs, cfg.yTickFmt);
  (cfg.hLines || []).forEach((l) => {
    root.append(svg("line", { x1: pad.l, x2: W - pad.r, y1: y(l.y), y2: y(l.y), stroke: l.color, "stroke-width": 1.5 }));
    const t = svg("text", { x: W - pad.r - 4, y: y(l.y) - 4, "text-anchor": "end", "font-size": 11, fill: css("--text-secondary") });
    t.textContent = l.label || "";
    root.append(t);
  });
  const allPts = [];
  cfg.series.forEach((s) => {
    s.points.forEach((p) => {
      const c = svg("circle", { cx: x(p[0]), cy: y(p[1]), r: 4, fill: s.color, stroke: css("--surface-1"), "stroke-width": 2 });
      root.append(c);
      allPts.push({ p, s, c });
    });
  });
  const hitRect = svg("rect", { x: pad.l, y: pad.t, width: W - pad.l - pad.r, height: H - pad.b - pad.t, fill: "transparent" });
  root.append(hitRect);
  let hot = null;
  hitRect.addEventListener("pointermove", (ev) => {
    const rect = root.getBoundingClientRect();
    const mxp = ev.clientX - rect.left, myp = ev.clientY - rect.top;
    let best = null;
    allPts.forEach((q) => {
      const dx = x(q.p[0]) - mxp, dy = y(q.p[1]) - myp;
      const d = Math.hypot(dx, dy);
      if (d < 24 && (!best || d < best.d)) best = { q, d };
    });
    if (hot && hot !== best?.q) hot.c.setAttribute("stroke-width", 2);
    hot = best?.q || null;
    if (hot) {
      hot.c.setAttribute("stroke-width", 3);
      const n = el("div");
      n.append(el("div", "t-title", fmtTs(hot.p[0])));
      const key = el("span", "dot-key"); key.style.background = hot.s.color;
      n.append(tipRow(key, hot.s.name, hot.p[1]));
      if (hot.p[2]) n.append(el("div", "sec", hot.p[2]));
      showTip(ev.clientX, ev.clientY, n);
    } else hideTip();
  });
  hitRect.addEventListener("pointerleave", () => { if (hot) hot.c.setAttribute("stroke-width", 2); hot = null; hideTip(); });
  cfg.el.replaceChildren(root);
}

function legendRow(container, items) {
  const lg = el("div", "legend");
  items.forEach((it) => {
    const k = el("span", "key");
    const sw = el("span", it.kind === "dot" ? "dot-key" : it.kind === "swatch" ? "swatch" : "line-key");
    sw.style.background = it.color;
    k.append(sw, el("span", null, it.name));
    lg.append(k);
  });
  container.append(lg);
}

function sevBadge(sev) {
  const s = SEV[sev] || SEV.info;
  const b = el("span", `badge ${sev}`);
  b.append(el("span", null, s.icon), el("span", null, s.label));
  return b;
}
function healthState(score) {
  if (score == null) return { cls: "info", label: "无数据" };
  if (score >= 80) return { cls: "good", label: "良好" };
  if (score >= 60) return { cls: "warn", label: "关注" };
  return { cls: "crit", label: "严重" };
}

/* ---------- shared state ---------- */
const state = { tab: "overview", device: null, rangeDays: 14, tableView: false, alertsStatus: "open", alertsDevice: "", evidence: null };
const main = $("#main");

function filterRow(nodes) {
  const r = el("div", "filter-row");
  nodes.forEach((n) => n && r.append(n));
  return r;
}
function selectEl(options, current, onChange) {
  const s = el("select");
  options.forEach(([v, label]) => {
    const o = el("option", null, label);
    o.value = v;
    if (String(v) === String(current)) o.selected = true;
    s.append(o);
  });
  s.addEventListener("change", () => onChange(s.value));
  return s;
}
function chipGroup(options, current, onChange) {
  const g = el("div", "chip-group");
  options.forEach(([v, label]) => {
    const c = el("button", "chip" + (String(v) === String(current) ? " active" : ""), label);
    c.addEventListener("click", () => onChange(v));
    g.append(c);
  });
  return g;
}
function rangeChips() {
  return chipGroup([[7, "7天"], [14, "14天"], [30, "30天"], [0, "全部"]], state.rangeDays, (v) => {
    state.rangeDays = Number(v);
    renderTab();
  });
}
function rangeBounds() {
  const end = Math.floor(Date.now() / 1000) + 3600;
  return state.rangeDays === 0 ? [0, end] : [end - state.rangeDays * 86400, end];
}

/* ================= overview ================= */
async function renderOverview() {
  const data = await api("/api/overview");
  $("#hdr-versions").textContent = `数据 v${data.data_version}`;
  const devs = data.devices;
  const scored = devs.filter((d) => d.health_score != null);
  const avg = scored.length ? scored.reduce((a, d) => a + d.health_score, 0) / scored.length : null;
  const open = devs.reduce((a, d) => a + d.open_alerts, 0);
  const ruls = devs.filter((d) => d.rul_hours != null).map((d) => d.rul_hours);

  const kpi = el("div", "kpi-row");
  [
    ["监控设备", devs.length, ""],
    ["平均健康分", avg == null ? "—" : avg.toFixed(1), ""],
    ["未决告警", open, open > 0 ? "需要处置" : "全部已处置"],
    ["最短剩余寿命", ruls.length ? fmtHours(Math.min(...ruls)) : "—", ruls.length ? "线性趋势外推" : "无退化趋势"],
  ].forEach(([label, value, sub]) => {
    const t = el("div", "stat-tile");
    t.append(el("div", "label", label), el("div", "value", String(value)), el("div", "delta sec", sub));
    kpi.append(t);
  });

  const grid = el("div", "device-grid");
  devs.forEach((d) => {
    const st = healthState(d.health_score);
    const card = el("div", "device-card");
    const head = el("div", "head");
    head.append(el("span", "name", `${d.device_id} · ${d.name}`), el("span", "asset", d.asset_id || ""));
    const meter = el("div", "meter");
    const fill = el("div", "fill");
    const mc = d.health_score == null ? css("--muted") : d.health_score >= 80 ? css("--status-good") : d.health_score >= 60 ? css("--status-warning") : css("--status-critical");
    fill.style.cssText = `width:${d.health_score ?? 0}%;background:${mc}`;
    meter.append(fill);
    const mrow = el("div", "meter-row");
    mrow.append(el("span", null, `健康分 ${d.health_score ?? "—"}`), el("span", null, `覆盖率 ${(d.coverage * 100).toFixed(0)}%`));
    const row2 = el("div", "meter-row");
    const badge = el("span", `badge ${st.cls}`, st.label);
    const al = el("span", `badge ${d.open_alerts ? (d.worst_severity === "crit" ? "crit" : "warn") : "neutral"}`,
      d.open_alerts ? `${SEV[d.worst_severity]?.icon || "●"} ${d.open_alerts} 条未决` : "无未决告警");
    row2.append(badge, al);
    const row3 = el("div", "meter-row");
    row3.append(
      el("span", null, `RUL ${fmtHours(d.rul_hours)}`),
      el("span", "muted", `数据v${d.data_version ?? "-"} 规则v${d.rule_version ?? "-"}`)
    );
    card.append(head, meter, mrow, row2, row3);
    card.addEventListener("click", () => { state.device = d.device_id; switchTab("timeline"); });
    grid.append(card);
  });
  main.replaceChildren(kpi, grid);
}

/* ================= timeline ================= */
async function renderTimeline() {
  const [ov, assetsData, batchesData] = await Promise.all([
    api("/api/overview"), api("/api/assets"), api("/api/batches"),
  ]);
  if (!state.device) state.device = ov.devices[0]?.device_id;
  const [start, end] = rangeBounds();

  const devOptions = ov.devices.map((d) => [d.device_id, `${d.device_id} · ${d.name}`]);
  Object.keys(assetsData.assets).forEach((a) => devOptions.push(["asset:" + a, `资产合并视图 · ${a}`]));
  const tableToggle = el("button", "chip" + (state.tableView ? " active" : ""), "表格视图");
  tableToggle.addEventListener("click", () => { state.tableView = !state.tableView; renderTab(); });

  main.replaceChildren(filterRow([
    el("span", "sec", "对象"),
    selectEl(devOptions, state.device, (v) => { state.device = v; renderTab(); }),
    el("span", "sec", "范围"),
    rangeChips(),
    tableToggle,
  ]));
  const content = el("div");
  main.append(content);

  if (String(state.device).startsWith("asset:")) {
    await renderAssetTimeline(content, state.device.slice(6), start, end);
  } else {
    await renderDeviceTimeline(content, state.device, start, end, batchesData.batches);
  }
}

async function renderDeviceTimeline(content, dev, start, end, batches) {
  const tl = await api(`/api/devices/${dev}/timeline?start=${start}&end=${end}`);
  const devBatches = batches.filter((b) => b.recompute.some((r) => r.device_id === dev));

  // --- health chart card
  const healthCard = el("div", "card");
  healthCard.append(el("h3", null, "健康分时间线"));
  const chartBox = el("div", "chart-wrap");

  const alerts = tl.alerts;
  const markers = alerts.map((a) => ({
    x: a.window_start, shape: a.severity === "warn" ? "tri" : "circle",
    color: SEV[a.severity]?.color || css("--muted"),
    title: `${SEV[a.severity]?.label || a.severity} · ${RULE_LABEL[a.rule_id] || a.rule_id}`,
    sub: `${fmtTs(a.window_start)} · ${a.metric || "综合"} · 状态:${STATUS_LABEL[a.status]}${a.disposition ? " · " + (DISP_LABEL[a.disposition] || a.disposition) : ""}`,
    alert: a,
  }));
  devBatches.filter((b) => b.kind === "backfill").forEach((b) => {
    markers.push({
      x: b.max_ts, shape: "down", color: css("--series-2"),
      title: `迟到数据批次 #${b.batch_id}`, sub: `${b.n_points} 点 · ${fmtTs(b.min_ts)} ~ ${fmtTs(b.max_ts)}`,
    });
  });
  tl.work_orders.forEach((w) => {
    markers.push({
      x: w.opened_at, shape: "diamond", color: css("--series-3"),
      title: `工单 · ${w.kind}`, sub: `${w.summary || ""} · ${fmtTs(w.opened_at)} · ${w.status}`,
    });
  });
  const vlines = tl.model_events
    .filter((m) => m.effective_from >= start && m.effective_from <= end)
    .map((m) => ({ x: m.effective_from, color: css("--series-7"), label: `换型→${m.model}` }));

  const legendItems = [];
  if (alerts.some((a) => a.severity === "crit")) legendItems.push({ name: "严重告警", color: css("--status-critical"), kind: "dot" });
  if (alerts.some((a) => a.severity === "warn")) legendItems.push({ name: "警告", color: css("--status-serious"), kind: "dot" });
  if (devBatches.some((b) => b.kind === "backfill")) legendItems.push({ name: "迟到批次", color: css("--series-2"), kind: "dot" });
  if (tl.work_orders.length) legendItems.push({ name: "工单", color: css("--series-3"), kind: "dot" });
  if (vlines.length) legendItems.push({ name: "换型", color: css("--series-7") });
  if (tl.downtime.length) legendItems.push({ name: "停机", color: css("--muted"), kind: "swatch" });
  if (legendItems.length) legendRow(healthCard, legendItems);
  healthCard.append(chartBox);

  const snaps = tl.snapshots;
  const healthPts = snaps.map((s) => [s.window_start, s.health_score]);
  const bands = tl.downtime.map((d) => ({ x0: d.start_ts, x1: d.end_ts || end }));

  if (state.tableView) {
    healthCard.append(snapshotTable(snaps));
  } else {
    lineChart({
      el: chartBox, height: 240, xDomain: [snaps[0]?.window_start ?? start, snaps[snaps.length - 1]?.window_end ?? end],
      yDomain: [0, 100], series: [{ name: "健康分", color: css("--series-1"), points: healthPts }],
      bands, vlines, markers,
      onMarkerClick: (m) => m.alert && showEvidence(dev, m.alert),
    });
  }
  content.append(healthCard);

  // --- coverage + RUL side by side
  const two = el("div", "two-col");
  const covCard = el("div", "card");
  covCard.append(el("h3", null, "数据覆盖率"));
  const covBox = el("div", "chart-wrap");
  covCard.append(covBox);
  lineChart({
    el: covBox, height: 140, xDomain: [snaps[0]?.window_start ?? start, snaps[snaps.length - 1]?.window_end ?? end],
    yDomain: [0, 1], series: [{ name: "覆盖率", color: css("--series-1"), points: snaps.map((s) => [s.window_start, s.coverage]), area: true }],
    yTickFmt: (v) => (v * 100).toFixed(0) + "%", yTipFmt: (v) => (v * 100).toFixed(1) + "%",
  });
  const rulCard = el("div", "card");
  rulCard.append(el("h3", null, "剩余寿命估计 (RUL)"));
  const rulBox = el("div", "chart-wrap");
  rulCard.append(rulBox);
  const rulPts = tl.rul.map((r) => [r.as_of, r.rul_hours]);
  if (rulPts.some((p) => p[1] != null)) {
    const mx = Math.max(...rulPts.filter((p) => p[1] != null).map((p) => p[1]));
    lineChart({
      el: rulBox, height: 140, xDomain: [snaps[0]?.window_start ?? start, snaps[snaps.length - 1]?.window_end ?? end],
      yDomain: [0, mx * 1.1], series: [{ name: "RUL(h)", color: css("--series-1"), points: rulPts }],
      yTipFmt: (v) => v.toFixed(0) + " h",
    });
  } else {
    rulBox.append(el("div", "empty", "无显著退化趋势 — RUL 不可估计"));
  }
  two.append(covCard, rulCard);
  content.append(two);

  // --- quality card
  const qCard = el("div", "card");
  qCard.append(el("h3", null, "数据质量构成"));
  const total = tl.quality.reduce((a, q) => a + q.n, 0);
  const bar = el("div", "seg-bar");
  const qItems = [];
  tl.quality.forEach((q) => {
    const seg = el("div", "seg");
    seg.style.cssText = `width:${(q.n / total) * 100}%;background:${QUALITY_COLOR[q.quality] || css("--muted")}`;
    bar.append(seg);
    qItems.push({ name: `${QUALITY_LABEL[q.quality] || q.quality} ${q.n}`, color: QUALITY_COLOR[q.quality] || css("--muted"), kind: "swatch" });
  });
  qCard.append(bar);
  legendRow(qCard, qItems);
  content.append(qCard);

  // --- alerts in range
  const aCard = el("div", "card");
  aCard.append(el("h3", null, `窗口告警 (${alerts.length})`, ));
  if (alerts.length) {
    const tbl = el("table", "data");
    tbl.innerHTML = "<thead><tr><th>时间</th><th>级别</th><th>规则</th><th>指标</th><th>状态</th><th>处置</th><th>版本</th></tr></thead>";
    const tb = el("tbody");
    alerts.forEach((a) => {
      const tr = el("tr", "clickable");
      const tdT = el("td", null, fmtTs(a.window_start));
      const tdS = el("td"); tdS.append(sevBadge(a.severity));
      tr.append(tdT, tdS, el("td", null, RULE_LABEL[a.rule_id] || a.rule_id),
        el("td", null, a.metric || "综合"), el("td", null, STATUS_LABEL[a.status]),
        el("td", null, a.disposition ? DISP_LABEL[a.disposition] : "—"),
        el("td", "muted", `d${a.data_version}/r${a.rule_version}`));
      tr.addEventListener("click", () => showEvidence(dev, a));
      tb.append(tr);
    });
    tbl.append(tb);
    aCard.append(tbl);
  } else aCard.append(el("div", "empty", "该时间段无告警"));
  content.append(aCard);

  // evidence container (filled on demand)
  const evCard = el("div");
  evCard.id = "evidence-slot";
  content.append(evCard);
  if (state.evidence) await showEvidence(dev, state.evidence);
}

function snapshotTable(snaps) {
  const tbl = el("table", "data");
  tbl.innerHTML = "<thead><tr><th>窗口</th><th class='num'>健康分</th><th class='num'>覆盖率</th><th>数据版本</th><th>规则版本</th></tr></thead>";
  const tb = el("tbody");
  snaps.forEach((s) => {
    const tr = el("tr");
    tr.append(el("td", null, fmtTs(s.window_start)),
      el("td", "num", s.health_score == null ? "—" : s.health_score.toFixed(1)),
      el("td", "num", (s.coverage * 100).toFixed(0) + "%"),
      el("td", null, `v${s.data_version}`), el("td", null, `v${s.rule_version}`));
    tb.append(tr);
  });
  tbl.append(tb);
  return tbl;
}

async function renderAssetTimeline(content, assetId, start, end) {
  const data = await api(`/api/assets/${assetId}/merged?metric=vibration&start=${start}&end=${end}`);
  const devIds = [...new Set(data.links.map((l) => l.device_id))];
  const colorOf = {};
  devIds.forEach((d, i) => (colorOf[d] = SERIES[i % 3]));

  const card = el("div", "card");
  card.append(el("h3", null, `资产 ${assetId} · 跨设备合并健康时间线`));
  legendRow(card, devIds.map((d) => ({ name: d, color: colorOf[d] })));
  const box = el("div", "chart-wrap");
  card.append(box);
  const series = devIds.map((d) => ({
    name: d, color: colorOf[d],
    points: data.health.filter((h) => h.device_id === d).map((h) => [h.window_start, h.health_score]),
  }));
  const vlines = data.links.filter((l) => l.valid_to).map((l) => ({ x: l.valid_to, color: css("--series-7"), label: "换机" }));
  lineChart({
    el: box, height: 240,
    xDomain: [data.health[0]?.window_start ?? start, data.health[data.health.length - 1]?.window_end ?? end],
    yDomain: [0, 100], series, vlines,
  });
  content.append(card);

  const pCard = el("div", "card");
  pCard.append(el("h3", null, "合并测点序列 (vibration)"));
  legendRow(pCard, devIds.map((d) => ({ name: d, color: colorOf[d], kind: "dot" })));
  const pBox = el("div", "chart-wrap");
  pCard.append(pBox);
  const vmax = Math.max(...data.series.map((p) => p.value), 1);
  scatterChart({
    el: pBox, height: 220,
    xDomain: [data.series[0]?.ts ?? start, data.series[data.series.length - 1]?.ts ?? end],
    yDomain: [0, vmax * 1.15],
    series: devIds.map((d) => ({
      name: d, color: colorOf[d],
      points: data.series.filter((p) => p.device_id === d).map((p) => [p.ts, p.value, QUALITY_LABEL[p.quality]]),
    })),
  });
  content.append(pCard);
}

/* ---------- evidence panel ---------- */
async function showEvidence(dev, alertRef) {
  state.evidence = alertRef;
  let slot = $("#evidence-slot");
  if (!slot) {
    switchTab("timeline");
    return;
  }
  const ev = await api(`/api/devices/${dev}/evidence?window_start=${alertRef.window_start}`);
  const card = el("div", "card");
  const h = el("h3", null, "异常证据");
  h.append(el("span", "sub", `${dev} · 窗口 ${fmtTs(alertRef.window_start)}`));
  card.append(h);

  if (!ev.alerts.length) {
    card.append(el("div", "empty", "该窗口当前无活动告警（可能已被后续数据修正）"));
    slot.replaceChildren(card);
    slot.scrollIntoView({ behavior: "smooth" });
    return;
  }
  const alert = ev.alerts.find((a) => a.metric) || ev.alerts[0];
  const metric = alert.metric || "vibration";
  const w0 = alert.window_start, w1 = alert.window_end;
  const pts = await api(`/api/devices/${dev}/points?metric=${metric}&start=${w0 - 7200}&end=${w1 + 7200}`);

  const grid = el("div", "evidence-grid");
  const left = el("div");
  const m = ev.snapshot?.metrics?.[metric];
  // scatter is an all-pairs form capped at 3 series: 实测 / 插补 / 采样修正(合并重复+漂移)
  const seriesDef = [
    { qs: ["ok"], name: "实测", color: css("--series-1") },
    { qs: ["gap_filled"], name: "缺测插补", color: css("--series-3") },
    { qs: ["drift_corrected", "dup_resolved"], name: "采样修正", color: css("--series-2") },
  ];
  const present = seriesDef.filter((s) => pts.points.some((p) => s.qs.includes(p.quality)));
  legendRow(left, present.map((s) => ({ name: s.name, color: s.color, kind: "dot" })));
  const chartBox = el("div", "chart-wrap");
  left.append(chartBox);

  const vals = pts.points.map((p) => p.value);
  const yMax = Math.max(...vals, m?.abs_max ?? 0, (m?.baseline?.mean ?? 0) + 3 * (m?.baseline?.std ?? 0)) * 1.1;
  const yMin = Math.min(...vals, (m?.baseline?.mean ?? 0) - 3 * (m?.baseline?.std ?? 0), 0);
  scatterChart({
    el: chartBox, height: 260, xDomain: [w0 - 7200, w1 + 7200], yDomain: [yMin, yMax],
    series: present.map((s) => ({
      name: s.name, color: s.color,
      points: pts.points.filter((p) => s.qs.includes(p.quality)).map((p) => [p.ts, p.value,
        p.quality === "drift_corrected" ? `漂移修正 ${p.drift_s}s` : p.quality === "dup_resolved" ? "重复采样去重" : null]),
    })),
    vBands: [{ x0: w0, x1: w1 }],
    hBands: m?.baseline?.mean != null ? [{ y0: m.baseline.mean - 2 * m.baseline.std, y1: m.baseline.mean + 2 * m.baseline.std }] : [],
    hLines: m ? [{ y: m.abs_max, color: css("--status-critical"), label: `绝对上限 ${m.abs_max}` }] : [],
  });
  grid.append(left);

  const right = el("div");
  const meta = el("table", "evidence-meta");
  const rows = [
    ["告警规则", RULE_LABEL[alert.rule_id] || alert.rule_id],
    ["级别", SEV[alert.severity]?.label || alert.severity],
    ["指标", metric],
    ["基线均值±2σ", m?.baseline?.mean != null ? `${m.baseline.mean.toFixed(2)} ± ${(2 * m.baseline.std).toFixed(2)} (n=${m.baseline.n})` : "—"],
    ["窗口 z 值", m?.z_agg ?? "—"],
    ["窗口最大/最小", m ? `${m.max} / ${Math.min(...vals).toFixed(2)}` : "—"],
    ["绑定版本", `数据 v${alert.data_version} · 规则 v${alert.rule_version}`],
  ];
  rows.forEach(([k, v]) => {
    const tr = el("tr");
    tr.append(el("td", null, k), el("td", null, String(v)));
    meta.append(tr);
  });
  right.append(meta);

  if (alert.evidence?.points?.length) {
    right.append(el("h3", null, "越限点"));
    const tbl = el("table", "data");
    tbl.innerHTML = "<thead><tr><th>时间</th><th class='num'>值</th><th class='num'>z</th></tr></thead>";
    const tb = el("tbody");
    alert.evidence.points.forEach((p) => {
      const tr = el("tr");
      tr.append(el("td", null, fmtTs(p.ts)), el("td", "num", p.value), el("td", "num", p.z));
      tb.append(tr);
    });
    tbl.append(tb);
    right.append(tbl);
  }

  // disposition form
  const form = el("div", "disp-form");
  const stSel = selectEl([["open", "未处理"], ["ack", "已确认"], ["resolved", "已解决"]], alert.status, () => {});
  const dpSel = selectEl([["", "处置方式…"], ["fixed", "已修复"], ["false_positive", "误报"], ["monitoring", "持续观察"], ["parts_ordered", "已订备件"]], alert.disposition || "", () => {});
  const note = el("input");
  note.placeholder = "处置备注";
  note.value = alert.disposition_note || "";
  const btn = el("button", "btn", "提交处置");
  btn.addEventListener("click", async () => {
    await api(`/api/alerts/${alert.id}/disposition`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ status: stSel.value, disposition: dpSel.value || null, note: note.value || null }),
    });
    state.evidence = null;
    renderTab();
  });
  form.append(el("span", "sec", "处置:"), stSel, dpSel, note, btn);
  right.append(form);
  grid.append(right);
  card.append(grid);
  slot.replaceChildren(card);
  slot.scrollIntoView({ behavior: "smooth" });
}

/* ================= alerts tab ================= */
async function renderAlerts() {
  const ov = await api("/api/overview");
  const devOpts = [["", "全部设备"], ...ov.devices.map((d) => [d.device_id, d.device_id])];
  main.replaceChildren(filterRow([
    chipGroup([["open", "未处理"], ["ack", "已确认"], ["resolved", "已解决"], ["", "全部"]], state.alertsStatus, (v) => { state.alertsStatus = v; renderTab(); }),
    selectEl(devOpts, state.alertsDevice, (v) => { state.alertsDevice = v; renderTab(); }),
  ]));
  const q = new URLSearchParams();
  if (state.alertsStatus) q.set("status", state.alertsStatus);
  if (state.alertsDevice) q.set("device_id", state.alertsDevice);
  const data = await api("/api/alerts?" + q);
  const card = el("div", "card");
  card.append(el("h3", null, `告警 (${data.alerts.length})`));
  if (!data.alerts.length) {
    card.append(el("div", "empty", "没有匹配的告警"));
    main.append(card);
    return;
  }
  const tbl = el("table", "data");
  tbl.innerHTML = "<thead><tr><th>时间</th><th>设备</th><th>级别</th><th>规则</th><th>指标</th><th>状态</th><th>处置</th><th>版本</th><th></th></tr></thead>";
  const tb = el("tbody");
  data.alerts.forEach((a) => {
    const tr = el("tr", "clickable");
    const tdS = el("td"); tdS.append(sevBadge(a.severity));
    const tdA = el("td");
    if (a.status !== "resolved") {
      const ack = el("button", "btn ghost", "确认");
      ack.addEventListener("click", async (ev) => {
        ev.stopPropagation();
        await api(`/api/alerts/${a.id}/disposition`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ status: "ack" }) });
        renderTab();
      });
      tdA.append(ack);
    }
    tr.append(el("td", null, fmtTs(a.window_start)), el("td", null, a.device_id), tdS,
      el("td", null, RULE_LABEL[a.rule_id] || a.rule_id), el("td", null, a.metric || "综合"),
      el("td", null, STATUS_LABEL[a.status]), el("td", null, a.disposition ? DISP_LABEL[a.disposition] : "—"),
      el("td", "muted", `d${a.data_version}/r${a.rule_version}`), tdA);
    tr.addEventListener("click", () => {
      state.device = a.device_id;
      state.evidence = { window_start: a.window_start };
      switchTab("timeline");
    });
    tb.append(tr);
  });
  tbl.append(tb);
  card.append(tbl);
  main.append(card);
}

/* ================= version diff ================= */
async function renderDiff() {
  const [ov, vers] = await Promise.all([api("/api/overview"), api("/api/versions")]);
  if (!state.diff) {
    state.diff = {
      device: ov.devices[0]?.device_id,
      v1: Math.max(1, vers.data_version - 1), r1: vers.rule_versions.find((r) => !r.active)?.rule_version ?? vers.rule_versions[0].rule_version,
      v2: vers.data_version, r2: vers.rule_versions.find((r) => r.active)?.rule_version ?? 999,
    };
  }
  const d = state.diff;
  const dvOpts = [...new Set(vers.batches.map((b) => b.data_version))].sort((a, b) => a - b).map((v) => [v, "数据 v" + v]);
  const rvOpts = vers.rule_versions.map((r) => [r.rule_version, `规则 v${r.rule_version}${r.active ? " (当前)" : ""}`]);
  const go = el("button", "btn", "对比");
  go.addEventListener("click", () => renderTab());
  main.replaceChildren(filterRow([
    el("span", "sec", "设备"),
    selectEl(ov.devices.map((x) => [x.device_id, x.device_id]), d.device, (v) => { d.device = v; }),
    el("span", "sec", "视图①"),
    selectEl(dvOpts, d.v1, (v) => { d.v1 = Number(v); }),
    selectEl(rvOpts, d.r1, (v) => { d.r1 = Number(v); }),
    el("span", "sec", "视图②"),
    selectEl(dvOpts, d.v2, (v) => { d.v2 = Number(v); }),
    selectEl(rvOpts, d.r2, (v) => { d.r2 = Number(v); }),
    go,
  ]));

  const res = await api(`/api/version-diff?device_id=${d.device}&v1=${d.v1}&v2=${d.v2}&r1=${d.r1}&r2=${d.r2}`);
  const kpi = el("div", "kpi-row");
  [
    ["健康分变化窗口", res.changed_windows.length],
    ["新增告警", res.alerts_added.length],
    ["消失告警", res.alerts_removed.length],
    ["RUL ①→②", `${fmtHours(res.rul1?.rul_hours)} → ${fmtHours(res.rul2?.rul_hours)}`],
  ].forEach(([label, value]) => {
    const t = el("div", "stat-tile");
    t.append(el("div", "label", label), el("div", "value", String(value)));
    kpi.append(t);
  });
  main.append(kpi);

  const wCard = el("div", "card");
  wCard.append(el("h3", null, "健康分变化窗口"));
  if (res.changed_windows.length) {
    const tbl = el("table", "data");
    tbl.innerHTML = "<thead><tr><th>窗口</th><th class='num'>视图①</th><th class='num'>视图②</th><th class='num'>变化</th></tr></thead>";
    const tb = el("tbody");
    res.changed_windows.forEach((w) => {
      const tr = el("tr");
      const delta = el("td", "num");
      if (w.delta != null) {
        delta.textContent = (w.delta > 0 ? "+" : "") + w.delta.toFixed(1);
        delta.className = "num delta " + (w.delta > 0 ? "up-good" : w.delta < 0 ? "down-bad" : "");
      } else delta.textContent = "—";
      tr.append(el("td", null, fmtTs(w.window_start)),
        el("td", "num", w.from == null ? "—" : w.from.toFixed(1)),
        el("td", "num", w.to == null ? "—" : w.to.toFixed(1)), delta);
      tb.append(tr);
    });
    tbl.append(tb);
    wCard.append(tbl);
  } else wCard.append(el("div", "empty", "两个视图的健康分完全一致"));
  main.append(wCard);

  const two = el("div", "two-col");
  [["新增告警", res.alerts_added], ["消失告警", res.alerts_removed]].forEach(([title, list]) => {
    const c = el("div", "card");
    c.append(el("h3", null, `${title} (${list.length})`));
    if (list.length) {
      const tbl = el("table", "data");
      tbl.innerHTML = "<thead><tr><th>时间</th><th>规则</th><th>指标</th><th>级别</th></tr></thead>";
      const tb = el("tbody");
      list.forEach((a) => {
        const tr = el("tr");
        const tdS = el("td"); tdS.append(sevBadge(a.severity));
        tr.append(el("td", null, fmtTs(a.window_start)), el("td", null, RULE_LABEL[a.rule_id] || a.rule_id),
          el("td", null, a.metric || "综合"), tdS);
        tb.append(tr);
      });
      tbl.append(tb);
      c.append(tbl);
    } else c.append(el("div", "empty", "无"));
    two.append(c);
  });
  main.append(two);

  // rule config compare
  const rc = el("div", "card");
  rc.append(el("h3", null, "规则配置对照"));
  const cfgRow = el("div", "two-col");
  [d.r1, d.r2].forEach((rv, i) => {
    const r = vers.rule_versions.find((x) => x.rule_version === rv);
    const box = el("div");
    box.append(el("h3", null, `视图${i === 0 ? "①" : "②"} · 规则 v${rv}`));
    const pre = el("pre", "muted");
    pre.style.cssText = "font-size:11px;overflow:auto;max-height:260px";
    pre.textContent = r ? JSON.stringify(JSON.parse(r.config_json), null, 1) : "（无此规则版本）";
    box.append(pre);
    cfgRow.append(box);
  });
  rc.append(cfgRow);
  main.append(rc);
}

/* ================= batches ================= */
async function renderBatches() {
  const data = await api("/api/batches");
  const card = el("div", "card");
  card.append(el("h3", null, "接入批次与增量重算记录"));
  const tbl = el("table", "data");
  tbl.innerHTML = "<thead><tr><th>批次</th><th>类型</th><th>到达时间</th><th>来源</th><th class='num'>点数</th><th>数据时间范围</th><th>数据版本</th><th>重算窗口</th></tr></thead>";
  const tb = el("tbody");
  data.batches.forEach((b) => {
    const tr = el("tr", "clickable");
    const tdK = el("td");
    tdK.append(el("span", "badge " + (b.kind === "backfill" ? "warn" : "neutral"), b.kind === "backfill" ? "迟到补录" : "实时"));
    const rec = b.recompute.map((r) => `${r.device_id}:${r.windows}`).join(" ");
    tr.append(el("td", null, "#" + b.batch_id), tdK, el("td", null, fmtTs(b.arrived_at)),
      el("td", null, b.source || "—"), el("td", "num", b.n_points),
      el("td", null, b.min_ts ? `${fmtTs(b.min_ts)} ~ ${fmtTs(b.max_ts)}` : "—"),
      el("td", null, "v" + b.data_version), el("td", "muted", rec || "—"));
    tr.addEventListener("click", () => showBatchDetail(b.batch_id));
    tb.append(tr);
  });
  tbl.append(tb);
  card.append(tbl);
  const slot = el("div");
  slot.id = "batch-detail";
  main.replaceChildren(card, slot);
}

async function showBatchDetail(batchId) {
  const data = await api(`/api/batches/${batchId}/recompute`);
  const slot = $("#batch-detail");
  const card = el("div", "card");
  card.append(el("h3", null, `批次 #${batchId} · 重算窗口明细 (${data.log.length})`));
  if (!data.log.length) {
    card.append(el("div", "empty", "该批次未触发窗口重算"));
    slot.replaceChildren(card);
    return;
  }
  const byDev = {};
  data.log.forEach((r) => (byDev[r.device_id] = byDev[r.device_id] || []).push(r));
  const t0 = Math.min(...data.log.map((r) => r.window_start));
  const t1 = Math.max(...data.log.map((r) => r.window_end));
  const reasonColor = { new_data: css("--series-1"), late_data: css("--series-2"), rule_change: css("--series-7") };
  const reasonLabel = { new_data: "新数据", late_data: "迟到数据", rule_change: "规则变更" };
  legendRow(card, Object.entries(reasonLabel).map(([k, v]) => ({ name: v, color: reasonColor[k], kind: "swatch" })));
  Object.entries(byDev).forEach(([dev, rows]) => {
    const wrap = el("div");
    wrap.style.marginBottom = "10px";
    wrap.append(el("div", "sec", `${dev} · ${rows.length} 个窗口 (${reasonLabel[rows[0].reason] || rows[0].reason})`));
    const strip = el("div", "strip-wrap");
    rows.forEach((r) => {
      const m = el("div", "strip-mark");
      m.style.cssText = `left:${((r.window_start - t0) / (t1 - t0)) * 100}%;width:${Math.max(0.4, ((r.window_end - r.window_start) / (t1 - t0)) * 100)}%;background:${reasonColor[r.reason]}`;
      m.title = `${fmtTs(r.window_start)} → ${fmtTs(r.window_end)}`;
      strip.append(m);
    });
    const axis = el("div", "strip-axis");
    axis.append(el("span", null, fmtTs(t0)), el("span", null, fmtTs(t1)));
    wrap.append(strip, axis);
    card.append(wrap);
  });
  const note = el("div", "muted");
  note.style.fontSize = "12px";
  note.textContent = "每个色块 = 一个被重算的健康窗口。迟到补录只重算其直接覆盖窗口与基线可达窗口，其余历史窗口保持原版本不变。";
  card.append(note);
  slot.replaceChildren(card);
  card.scrollIntoView({ behavior: "smooth" });
}

/* ================= router ================= */
const TABS = { overview: renderOverview, timeline: renderTimeline, alerts: renderAlerts, diff: renderDiff, batches: renderBatches };
async function renderTab() {
  main.classList.add("loading");
  try {
    await TABS[state.tab]();
  } catch (e) {
    main.replaceChildren(el("div", "empty", "加载失败: " + e.message));
    console.error(e);
  } finally {
    main.classList.remove("loading");
  }
}
function switchTab(tab) {
  state.tab = tab;
  document.querySelectorAll("#tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  renderTab();
}
document.querySelectorAll("#tabs button").forEach((b) => b.addEventListener("click", () => switchTab(b.dataset.tab)));
$("#theme-toggle").addEventListener("click", () => {
  const r = document.documentElement;
  r.dataset.theme = r.dataset.theme === "dark" ? "light" : "dark";
  renderTab();
});
renderTab();
