/* Shared browser helpers: safe DOM, provenance badges, API, WebSocket and charts. */

export const WINDOW_MS = {
  "5m": 5 * 60 * 1000,
  "15m": 15 * 60 * 1000,
  "1h": 60 * 60 * 1000,
  "6h": 6 * 60 * 60 * 1000,
  "24h": 24 * 60 * 60 * 1000,
};

export const WINDOW_LABEL = {
  "5m": "5 分钟",
  "15m": "15 分钟",
  "1h": "1 小时",
  "6h": "6 小时",
  "24h": "24 小时",
};

export const PALETTE = {
  system: "#f5b942",
  cpu: "#4cc9f0",
  gpuTotal: "#69f0ae",
  gpu: ["#b388ff", "#ff6f91", "#ffd166", "#7bdff2"],
  energy: "#f5b942",
};

const QUALITY_LABEL = {
  measured: "Measured",
  estimated: "Estimated",
  unavailable: "Unavailable",
};

const MODE_LABEL = {
  measured: "实测",
  estimated: "估算",
  unavailable: "不可用",
};

const DATE_TIME_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

const CLOCK_FORMAT = new Intl.DateTimeFormat("zh-CN", {
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hour12: false,
});

/* ------------------------------------------------------------------ DOM */

export function text(element, value, fallback = "") {
  if (!element) return;
  element.textContent = value === null || value === undefined || value === "" ? fallback : String(value);
}

export function clear(element) {
  if (element) element.replaceChildren();
}

export function createEl(tag, className = "", value = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (value !== "" && value !== null && value !== undefined) element.textContent = String(value);
  return element;
}

export function appendText(parent, tag, className, value) {
  const element = createEl(tag, className, value);
  parent.append(element);
  return element;
}

/* ------------------------------------------------------------------ tooltips */

let tooltipElement = null;
let tooltipTarget = null;

function ensureTooltip() {
  if (tooltipElement) return tooltipElement;
  tooltipElement = document.createElement("div");
  tooltipElement.className = "ui-tooltip";
  tooltipElement.id = "ui-tooltip";
  tooltipElement.setAttribute("role", "tooltip");
  tooltipElement.hidden = true;
  document.body.append(tooltipElement);
  return tooltipElement;
}

function hideTooltip() {
  if (tooltipTarget) tooltipTarget.removeAttribute("aria-describedby");
  tooltipTarget = null;
  if (tooltipElement) tooltipElement.hidden = true;
}

function showTooltip(target) {
  const content = target.dataset.tip;
  if (!content) return;
  if (tooltipTarget !== target) hideTooltip();
  const tooltip = ensureTooltip();
  tooltip.textContent = content;
  tooltip.hidden = false;
  tooltipTarget = target;
  target.setAttribute("aria-describedby", "ui-tooltip");

  const anchor = target.getBoundingClientRect();
  const box = tooltip.getBoundingClientRect();
  const margin = 8;
  const left = Math.min(
    Math.max(margin, anchor.left + anchor.width / 2 - box.width / 2),
    Math.max(margin, window.innerWidth - box.width - margin),
  );
  let top = anchor.top - box.height - margin;
  if (top < margin) top = anchor.bottom + margin;
  if (top + box.height > window.innerHeight - margin) {
    top = Math.max(margin, anchor.top - box.height - margin);
  }
  tooltip.style.left = `${Math.round(left)}px`;
  tooltip.style.top = `${Math.round(top)}px`;
}

function initTooltips() {
  if (initTooltips.done) return;
  initTooltips.done = true;

  document.addEventListener("pointerover", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-tip]") : null;
    if (!target) return;
    if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return;
    showTooltip(target);
  });
  document.addEventListener("pointerout", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-tip]") : null;
    if (!target || target !== tooltipTarget) return;
    if (event.relatedTarget instanceof Node && target.contains(event.relatedTarget)) return;
    hideTooltip();
  });
  document.addEventListener("focusin", (event) => {
    const target = event.target instanceof Element ? event.target.closest("[data-tip]") : null;
    if (target) showTooltip(target);
  });
  document.addEventListener("focusout", (event) => {
    if (event.target === tooltipTarget) hideTooltip();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") hideTooltip();
  });
  window.addEventListener("scroll", hideTooltip, { passive: true });
  window.addEventListener("resize", hideTooltip, { passive: true });
}

initTooltips();


/* ------------------------------------------------------------------ numbers */

export function isNumber(value) {
  return typeof value === "number" && Number.isFinite(value);
}

export function formatNumber(value, digits = 1) {
  if (!isNumber(value)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: 0,
    maximumFractionDigits: Math.max(0, digits),
  }).format(value);
}

export function formatFixed(value, digits = 1) {
  if (!isNumber(value)) return "—";
  return new Intl.NumberFormat("zh-CN", {
    minimumFractionDigits: Math.max(0, digits),
    maximumFractionDigits: Math.max(0, digits),
  }).format(value);
}

export function formatPercent(value, digits = 1) {
  return isNumber(value) ? `${formatNumber(value, digits)}%` : "—";
}

export function formatBytes(value, digits = 1) {
  if (!isNumber(value) || value < 0) return "—";
  const units = ["B", "KiB", "MiB", "GiB", "TiB", "PiB"];
  let amount = value;
  let index = 0;
  while (amount >= 1024 && index < units.length - 1) {
    amount /= 1024;
    index += 1;
  }
  return `${formatNumber(amount, digits)} ${units[index]}`;
}

export function formatSpeed(mbps, digits = 2) {
  return isNumber(mbps) ? `${formatNumber(mbps, digits)} Mb/s` : "—";
}

export function formatDuration(seconds) {
  if (!isNumber(seconds) || seconds < 0) return "—";
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  if (days > 0) return `${days} 天 ${hours} 小时`;
  if (hours > 0) return `${hours} 小时 ${minutes} 分`;
  return `${minutes} 分`;
}

export function formatDateTime(value) {
  if (!value) return "—";
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return DATE_TIME_FORMAT.format(date);
}

export function formatClock(value) {
  const date = value instanceof Date ? value : new Date(value ?? Date.now());
  if (Number.isNaN(date.getTime())) return "—";
  return CLOCK_FORMAT.format(date);
}

export function formatHour(value) {
  const date = value instanceof Date ? value : new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatHourShort(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "—";
  return new Intl.DateTimeFormat("zh-CN", {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  }).format(date);
}

export function formatCurrency(value, currency) {
  if (!isNumber(value)) return "—";
  const code = (currency || "").trim().toUpperCase();
  if (code) {
    try {
      return new Intl.NumberFormat("zh-CN", {
        style: "currency",
        currency: code,
        maximumFractionDigits: 2,
      }).format(value);
    } catch {
      /* fall through to a plain suffix */
    }
  }
  return `${formatNumber(value, 2)}${code ? ` ${code}` : ""}`;
}

/* ------------------------------------------------------------------ provenance */

export function measurementValue(measurement) {
  return measurement && isNumber(measurement.value) ? measurement.value : null;
}

export function measurementQuality(measurement) {
  return measurement && measurement.quality ? measurement.quality : "unavailable";
}

export function measurementTip(measurement, scope = null) {
  if (!measurement) return "no data collected";
  const lines = [];
  if (measurement.source) lines.push(`source: ${measurement.source}`);
  if (measurement.timestamp) lines.push(`time: ${formatDateTime(measurement.timestamp)}`);
  const effectiveScope = scope ?? measurement.scope;
  if (effectiveScope) lines.push(`scope: ${effectiveScope}`);
  if (measurement.error) lines.push(`error: ${measurement.error}`);
  if (Array.isArray(measurement.missing) && measurement.missing.length) {
    lines.push(`missing: ${measurement.missing.join(", ")}`);
  }
  if (!lines.length) lines.push(`quality: ${measurementQuality(measurement)}`);
  return lines.join("\n");
}

export function qualityBadge(measurement, { scope = null, label = null } = {}) {
  const quality = measurementQuality(measurement);
  const badge = createEl("span", `badge badge--${quality}`);
  badge.textContent = label || QUALITY_LABEL[quality] || "Unavailable";
  const tip = measurementTip(measurement, scope);
  badge.dataset.tip = tip;
  badge.tabIndex = 0;
  badge.setAttribute("aria-label", `${badge.textContent}: ${tip.replace(/\n/g, "; ")}`);
  return badge;
}

export function modeLabel(mode) {
  return MODE_LABEL[mode] || mode || "—";
}

export function renderMeasurement({
  valueElement,
  unitElement = null,
  badgeElement = null,
  measurement,
  digits = 1,
  fallbackUnit = "W",
  scope = null,
  badgeLabel = null,
}) {
  const value = measurementValue(measurement);
  text(valueElement, value === null ? "—" : formatFixed(value, digits));
  if (unitElement) text(unitElement, measurement?.unit || fallbackUnit);
  if (badgeElement) {
    badgeElement.replaceChildren(qualityBadge(measurement, { scope, label: badgeLabel }));
  }
}

export function gpuTotalMeasurement(snapshot) {
  const timestamp = snapshot?.timestamp ?? new Date().toISOString();
  if (!snapshot?.gpu_available) {
    return {
      value: null,
      unit: "W",
      quality: "unavailable",
      source: "gpu",
      timestamp,
      error: snapshot?.gpu_error || "GPU enumeration unavailable",
      missing: ["gpu"],
    };
  }
  const gpus = Array.isArray(snapshot.gpu) ? snapshot.gpu : [];
  if (gpus.length === 0) {
    return {
      value: 0,
      unit: "W",
      quality: "measured",
      source: "gpu:none-in-total",
      timestamp,
      missing: [],
    };
  }
  const counted = gpus.filter((gpu) => gpu?.include_in_system_total !== false);
  if (counted.length === 0) {
    return {
      value: 0,
      unit: "W",
      quality: "measured",
      source: "gpu:none-in-total",
      timestamp,
      missing: [],
    };
  }
  let total = 0;
  for (const gpu of counted) {
    const power = gpu?.power;
    if (!isNumber(power?.value) || power.quality !== "measured") {
      return {
        value: null,
        unit: "W",
        quality: "unavailable",
        source: "gpu:sum",
        timestamp,
        error: `GPU ${gpu?.uuid || ""} power unavailable`,
        missing: counted.map((item) => `gpu:${item.uuid}`),
      };
    }
    total += power.value;
  }
  return {
    value: total,
    unit: "W",
    quality: "measured",
    source: "gpu:sum",
    timestamp,
    missing: [],
  };
}

export function collectorStateVariant(state) {
  if (state === "ok") return "ok";
  if (state === "error") return "error";
  if (state === "stale" || state === "starting") return "warn";
  return "muted";
}

/* ------------------------------------------------------------------ API */

export async function apiGet(path, params = null) {
  const url = new URL(path, window.location.origin);
  if (params) {
    for (const [key, value] of Object.entries(params)) {
      if (value !== null && value !== undefined && value !== "") {
        url.searchParams.set(key, String(value));
      }
    }
  }
  const response = await fetch(url, {
    headers: { Accept: "application/json" },
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = `HTTP ${response.status}`;
    try {
      const payload = await response.json();
      if (payload && payload.detail) detail = String(payload.detail);
    } catch {
      /* keep the status text */
    }
    throw new Error(detail);
  }
  return response.json();
}

export async function fetchHistory(windowKey, seriesIds) {
  return apiGet("/api/v1/power/history", {
    window: windowKey,
    series: seriesIds.join(","),
  });
}

export function seriesMap(payload) {
  const result = new Map();
  for (const item of payload?.series || []) {
    result.set(item.id, item);
  }
  return result;
}

export function sourceLabel(source) {
  if (source === "raw") return "原始样本";
  if (source === "aggregate_1m") return "1 分钟聚合";
  if (source === "aggregate_1h") return "1 小时聚合";
  return source || "未知来源";
}

export function pushPoint(points, timestamp, value, capacity = 2000) {
  if (!Number.isFinite(timestamp)) return points;
  if (points.length) {
    const last = points[points.length - 1];
    if (timestamp === last[0]) {
      last[1] = value;
      return points;
    }
    if (timestamp < last[0]) return points;
  }
  points.push([timestamp, value]);
  if (points.length > capacity) points.splice(0, points.length - capacity);
  return points;
}

/* ------------------------------------------------------------------ WebSocket */

const SOCKET_URL = `${window.location.protocol === "https:" ? "wss" : "ws"}://${window.location.host}/ws/realtime`;
const BACKOFF_MS = [1000, 2000, 4000, 8000, 15000];
const STATUS_TEXT = {
  idle: "Offline",
  connecting: "Connecting",
  live: "Live",
  reconnecting: "Reconnecting",
  offline: "Offline",
};

function renderConnectionStatus(status, detail = "") {
  const box = document.getElementById("conn");
  if (!box) return;
  box.dataset.state = status;
  const label = document.getElementById("conn-text");
  if (label) label.textContent = STATUS_TEXT[status] || status;
  box.title = detail ? `${STATUS_TEXT[status] || status}: ${detail}` : STATUS_TEXT[status] || status;
}

function renderLastUpdate(timestamp) {
  const element = document.getElementById("last-update");
  if (!element) return;
  const value = timestamp ? new Date(timestamp) : new Date();
  text(element, `更新 ${formatClock(value)}`);
  element.title = formatDateTime(value);
}

class RealtimeClient extends EventTarget {
  constructor() {
    super();
    this.socket = null;
    this.running = false;
    this.attempt = 0;
    this.timer = null;
    this.connectedOnce = false;
    this.online = navigator.onLine !== false;
    this.lastMessageAt = null;

    window.addEventListener("online", () => {
      this.online = true;
      if (this.socket && this.socket.readyState === WebSocket.OPEN) {
        this._setStatus("live");
      } else {
        this._connectNow();
      }
    });
    window.addEventListener("offline", () => {
      this.online = false;
      this._clearTimer();
      this._setStatus("offline", "browser is offline");
    });
    document.addEventListener("visibilitychange", () => {
      if (document.visibilityState !== "visible") return;
      if (!this.socket || this.socket.readyState === WebSocket.CLOSED || this.socket.readyState === WebSocket.CLOSING) {
        this._connectNow();
      }
    });
    window.addEventListener("pagehide", () => this.stop());
    window.addEventListener("pageshow", (event) => {
      if (event.persisted) this.start();
    });
  }

  start() {
    if (this.running) return;
    this.running = true;
    this._connect();
  }

  stop() {
    this.running = false;
    this._clearTimer();
    if (this.socket) {
      const socket = this.socket;
      this.socket = null;
      try {
        socket.close();
      } catch {
        /* already closed */
      }
    }
    this._setStatus("offline", "page stopped");
  }

  _clearTimer() {
    if (this.timer !== null) {
      window.clearTimeout(this.timer);
      this.timer = null;
    }
  }

  _setStatus(status, detail = "") {
    renderConnectionStatus(status, detail);
    this.dispatchEvent(new CustomEvent("status", { detail: { status, detail } }));
  }

  _connect() {
    if (!this.running) return;
    if (!this.online) {
      this._setStatus("offline", "browser is offline");
      return;
    }
    if (
      this.socket &&
      (this.socket.readyState === WebSocket.OPEN || this.socket.readyState === WebSocket.CONNECTING)
    ) {
      return;
    }
    this._clearTimer();
    this._setStatus(this.connectedOnce ? "reconnecting" : "connecting");

    let socket;
    try {
      socket = new WebSocket(SOCKET_URL);
    } catch (error) {
      this._scheduleReconnect(error instanceof Error ? error.message : "WebSocket unavailable");
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => {
      if (this.socket !== socket) return;
      this.attempt = 0;
      this._setStatus("live");
      if (this.connectedOnce) {
        this.dispatchEvent(new CustomEvent("reconnected"));
      }
      this.connectedOnce = true;
    });

    socket.addEventListener("message", (event) => {
      if (this.socket !== socket) return;
      this.lastMessageAt = Date.now();
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (!message || typeof message !== "object") return;
      if (message.type === "hello") {
        this._setStatus("live");
        this.dispatchEvent(new CustomEvent("hello", { detail: message }));
        return;
      }
      if (message.type === "ping") {
        this.dispatchEvent(new CustomEvent("heartbeat", { detail: message }));
        return;
      }
      if (message.type === "snapshot") {
        renderLastUpdate(message.timestamp);
        this.dispatchEvent(new CustomEvent("snapshot", { detail: message }));
      }
    });

    socket.addEventListener("close", () => {
      if (this.socket !== socket) return;
      this.socket = null;
      if (!this.running) return;
      this._scheduleReconnect();
    });

    socket.addEventListener("error", () => {
      /* close follows; backoff is handled there */
    });
  }

  _scheduleReconnect(detail = "") {
    if (!this.running) return;
    if (!this.online) {
      this._setStatus("offline", "browser is offline");
      return;
    }
    this._clearTimer();
    const base = BACKOFF_MS[Math.min(this.attempt, BACKOFF_MS.length - 1)];
    const jitter = 0.8 + Math.random() * 0.4;
    const delay = Math.round(base * jitter);
    this.attempt += 1;
    this._setStatus("reconnecting", detail || `retry in ${delay} ms`);
    this.timer = window.setTimeout(() => {
      this.timer = null;
      this._connect();
    }, delay);
  }

  _connectNow() {
    if (!this.running) return;
    this._clearTimer();
    this.attempt = 0;
    this._connect();
  }
}

let sharedClient = null;

export function getRealtime() {
  if (!sharedClient) sharedClient = new RealtimeClient();
  return sharedClient;
}

/* ------------------------------------------------------------------ charts */

const AXIS_COLOR = "#8b9bb4";
const GRID_COLOR = "rgba(34, 48, 74, 0.62)";

function chartTextStyle() {
  return {
    color: "#c7d3e6",
    fontFamily: 'Inter, "Segoe UI", "PingFang SC", system-ui, sans-serif',
  };
}

function tooltipStyle() {
  return {
    backgroundColor: "rgba(6, 10, 18, 0.97)",
    borderColor: "#22304a",
    borderWidth: 1,
    padding: [9, 11],
    textStyle: { color: "#e8eef9", fontSize: 12 },
    extraCssText: "box-shadow: 0 12px 28px rgba(0,0,0,.45); border-radius: 9px;",
  };
}

export function mountChart(element) {
  if (!window.echarts) throw new Error("ECharts 未加载");
  const instance = window.echarts.init(element, null, { renderer: "canvas" });
  const observer = new ResizeObserver(() => instance.resize());
  observer.observe(element);
  window.addEventListener("resize", () => instance.resize(), { passive: true });
  return {
    instance,
    resize: () => instance.resize(),
    dispose: () => {
      observer.disconnect();
      instance.dispose();
    },
  };
}

export function powerChartOption({ series, fromMs, toMs }) {
  return {
    animation: false,
    backgroundColor: "transparent",
    textStyle: chartTextStyle(),
    grid: { left: 54, right: 18, top: 44, bottom: 34, containLabel: false },
    legend: {
      top: 2,
      left: 0,
      itemWidth: 14,
      itemHeight: 8,
      itemGap: 14,
      textStyle: { color: AXIS_COLOR, fontSize: 11 },
    },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "line", lineStyle: { color: "#3b4d6b" } },
      ...tooltipStyle(),
      valueFormatter: (value) => (isNumber(value) ? `${formatFixed(value, 1)} W` : "—"),
    },
    xAxis: {
      type: "time",
      min: fromMs,
      max: toMs,
      axisLine: { lineStyle: { color: "#22304a" } },
      axisTick: { show: false },
      axisLabel: { color: AXIS_COLOR, hideOverlap: true, fontSize: 10 },
      splitLine: { show: false },
    },
    yAxis: {
      type: "value",
      name: "W",
      nameTextStyle: { color: AXIS_COLOR, fontSize: 10, padding: [0, 0, 0, -22] },
      axisLabel: { color: AXIS_COLOR, fontSize: 10 },
      splitLine: { lineStyle: { color: GRID_COLOR } },
    },
    series: series.map((item) => ({
      id: item.id,
      name: item.label,
      type: "line",
      showSymbol: false,
      symbol: "circle",
      symbolSize: 4,
      connectNulls: false,
      animation: false,
      data: item.points,
      lineStyle: { color: item.color, width: item.id === "system" ? 2.4 : 1.7 },
      itemStyle: { color: item.color },
      emphasis: { focus: "series" },
      areaStyle:
        item.id === "system"
          ? {
              color: {
                type: "linear",
                x: 0,
                y: 0,
                x2: 0,
                y2: 1,
                colorStops: [
                  { offset: 0, color: "rgba(245,185,66,0.18)" },
                  { offset: 1, color: "rgba(245,185,66,0.01)" },
                ],
              },
            }
          : undefined,
    })),
  };
}

export function barChartOption({ categories, values, unit = "kWh", digits = 2, color = PALETTE.energy }) {
  return {
    animation: false,
    backgroundColor: "transparent",
    textStyle: chartTextStyle(),
    grid: { left: 58, right: 18, top: 26, bottom: 42 },
    tooltip: {
      trigger: "axis",
      axisPointer: { type: "shadow" },
      ...tooltipStyle(),
      valueFormatter: (value) => (isNumber(value) ? `${formatNumber(value, digits)} ${unit}` : "—"),
    },
    xAxis: {
      type: "category",
      data: categories,
      axisLine: { lineStyle: { color: "#22304a" } },
      axisTick: { show: false },
      axisLabel: { color: AXIS_COLOR, hideOverlap: true, fontSize: 10 },
    },
    yAxis: {
      type: "value",
      name: unit,
      nameTextStyle: { color: AXIS_COLOR, fontSize: 10 },
      axisLabel: { color: AXIS_COLOR, fontSize: 10 },
      splitLine: { lineStyle: { color: GRID_COLOR } },
    },
    series: [
      {
        type: "bar",
        name: unit,
        data: values,
        barMaxWidth: 30,
        itemStyle: { color, borderRadius: [4, 4, 0, 0] },
        emphasis: { itemStyle: { color } },
      },
    ],
  };
}

/**
 * Line chart with a range selector.
 *
 * 5 m views append WebSocket snapshots into per-series ring buffers (2000
 * points).  Longer windows load once from REST and are refreshed every 60 s,
 * so the browser never accumulates points forever.
 */
export class HistoryView {
  constructor({ element, rangeGroup = null, definitions, window: initialWindow = "5m", metaElement = null, onError = null }) {
    this.mount = mountChart(element);
    this.chart = this.mount.instance;
    this.rangeGroup = rangeGroup;
    this.definitions = definitions;
    this.window = initialWindow;
    this.metaElement = metaElement;
    this.onError = onError;
    this.points = new Map();
    this.payload = null;
    this.token = 0;
    this.loaded = false;
    this.refreshTimer = null;

    this._bindRanges();
    this._setActiveRange();
    this.refreshTimer = window.setInterval(() => {
      if (this.window !== "5m" && document.visibilityState === "visible") {
        this.load(this.window, { silent: true });
      }
    }, 60_000);
  }

  _bindRanges() {
    if (!this.rangeGroup) return;
    for (const button of this.rangeGroup.querySelectorAll("[data-window]")) {
      button.addEventListener("click", () => {
        const next = button.dataset.window;
        if (!WINDOW_MS[next] || next === this.window) return;
        this.load(next);
      });
    }
  }

  _setActiveRange() {
    if (!this.rangeGroup) return;
    for (const button of this.rangeGroup.querySelectorAll("[data-window]")) {
      button.classList.toggle("is-active", button.dataset.window === this.window);
      button.setAttribute("aria-pressed", button.dataset.window === this.window ? "true" : "false");
    }
  }

  setDefinitions(definitions) {
    this.definitions = definitions;
    this.load(this.window);
  }

  async load(nextWindow = this.window, { silent = false } = {}) {
    const previous = this.window;
    const switching = nextWindow !== previous || !this.loaded || !silent;
    this.window = nextWindow;
    this._setActiveRange();
    const token = ++this.token;

    if (switching) {
      this.chart.clear();
      this.points.clear();
      for (const definition of this.definitions) this.points.set(definition.id, []);
      if (this.metaElement && !silent) this.metaElement.textContent = "加载中…";
    }

    try {
      const payload = await fetchHistory(this.window, this.definitions.map((item) => item.id));
      if (token !== this.token) return;
      this.payload = payload;
      const byId = seriesMap(payload);
      for (const definition of this.definitions) {
        const points = byId.get(definition.id)?.points ?? [];
        this.points.set(definition.id, points.slice(-2000));
      }
      this.loaded = true;
      this.render(true);
      const seriesQuality = payload.series?.find((item) => item.quality)?.quality;
      const hasValues = (payload.series || []).some((item) =>
        (item.points || []).some((point) => isNumber(point[1])),
      );
      const qualityLabel = hasValues ? QUALITY_LABEL[seriesQuality] || seriesQuality : "Unavailable";
      const qualityNote = seriesQuality ? ` · ${qualityLabel}` : "";
      text(
        this.metaElement,
        `${sourceLabel(payload.source)} · 分辨率 ${payload.resolution}${qualityNote} · ${WINDOW_LABEL[this.window] || this.window}`,
      );
    } catch (error) {
      if (token !== this.token) return;
      this.loaded = true;
      this.payload = {
        source: "live",
        resolution: "1s",
        from_ms: Date.now() - (WINDOW_MS[this.window] || 300_000),
        to_ms: Date.now(),
      };
      this.render(true);
      text(this.metaElement, `历史不可用：${error.message}（仅显示实时数据）`);
      if (this.onError) this.onError(error);
    }
  }

  append(snapshot) {
    if (this.window !== "5m") return;
    const timestamp = Date.parse(snapshot?.timestamp);
    if (!Number.isFinite(timestamp)) return;
    for (const definition of this.definitions) {
      const measurement = definition.extract ? definition.extract(snapshot) : null;
      let points = this.points.get(definition.id);
      if (!points) {
        points = [];
        this.points.set(definition.id, points);
      }
      pushPoint(points, timestamp, measurementValue(measurement), 2000);
    }
    this.render(false);
  }

  render(notMerge = false) {
    if (!this.definitions.length) return;
    const span = WINDOW_MS[this.window] || 300_000;
    const now = Date.now();
    const series = this.definitions.map((definition) => ({
      id: definition.id,
      label: definition.label,
      color: definition.color,
      points: this.points.get(definition.id) || [],
    }));
    this.chart.setOption(powerChartOption({ series, fromMs: now - span, toMs: now }), {
      notMerge,
      lazyUpdate: true,
    });
  }

  refresh() {
    this.load(this.window);
  }
}
