import {
  apiGet,
  barChartOption,
  clear,
  createEl,
  fetchHistory,
  formatClock,
  formatCurrency,
  formatDateTime,
  formatFixed,
  formatHourShort,
  formatNumber,
  getRealtime,
  isNumber,
  measurementQuality,
  mountChart,
  qualityBadge,
  renderMeasurement,
  text,
} from "../core.js";

const REFRESH_MS = 60_000;
const DAILY_DAYS = 30;
const HOURS = 24;

function componentLabel(name) {
  if (name === "cpu") return "CPU";
  if (name === "gpu") return "GPU";
  if (String(name).startsWith("gpu:")) return `GPU ${String(name).slice(4, 12)}`;
  return String(name);
}

const state = {
  summary: null,
  config: null,
  monthlyCost: null,
  monthlyCostYear: null,
  monthlyCostChart: null,
  monthlyCostRequest: 0,
  dailyChart: null,
  hourlyChart: null,
  refreshTimer: null,
  loading: false,
};

/* ------------------------------------------------------------------ badges */

function energyBadge(entry, source, scope = "energy") {
  const timestamp = state.summary?.timestamp || new Date().toISOString();
  const measurement = {
    value: entry?.energy_kwh ?? null,
    unit: "kWh",
    quality: entry?.quality || "unavailable",
    source,
    timestamp,
    scope,
    missing: [],
  };
  if (measurement.quality === "unavailable") {
    measurement.error = "no integrable samples in this period";
  }
  return qualityBadge(measurement, { scope });
}

function seriesBadge(series, payload) {
  const measurement = {
    value: null,
    unit: series?.unit || "W",
    quality: series?.quality || "unavailable",
    source: payload?.source ? `history:${payload.source}` : "history",
    timestamp: payload?.to_ms ? new Date(payload.to_ms).toISOString() : new Date().toISOString(),
    scope: series?.scope || "dc_components",
    missing: [],
  };
  if (series?.note) measurement.error = series.note;
  return qualityBadge(measurement, { scope: measurement.scope });
}

/* ------------------------------------------------------------------ summary */

function priceEnabled() {
  if (typeof state.config?.electricity?.enabled === "boolean") return state.config.electricity.enabled;
  if (typeof state.monthlyCost?.electricity_enabled === "boolean") {
    return state.monthlyCost.electricity_enabled;
  }
  if (typeof state.summary?.electricity_enabled === "boolean") return state.summary.electricity_enabled;
  return false;
}

function currency() {
  return (
    state.summary?.currency ||
    state.monthlyCost?.currency ||
    state.config?.electricity?.currency ||
    ""
  );
}

function renderPrice() {
  const pill = document.getElementById("price-pill");
  if (!pill) return;
  const price = state.summary?.price_per_kwh ?? state.config?.electricity?.price_per_kwh;
  if (priceEnabled()) {
    pill.textContent = `电价 ${formatCurrency(price, currency())} / kWh`;
    pill.className = "pill pill--ok";
  } else {
    pill.textContent = "电价 未启用";
    pill.className = "pill pill--muted";
  }
}

function costLabel(entry) {
  if (!priceEnabled()) return "费用 —（电价未启用）";
  return entry?.cost === null || entry?.cost === undefined
    ? "费用 —"
    : `费用 ${formatCurrency(entry.cost, currency())}`;
}

function monthlyCostLabel(entry) {
  if (!priceEnabled()) return "—（电价未启用）";
  return isNumber(entry?.cost) ? formatCurrency(entry.cost, currency()) : "—";
}

function qualityText(quality) {
  if (quality === "measured") return "实测";
  if (quality === "estimated") return "估算";
  return "无数据";
}

function detailLabel(entry) {
  if (!entry) return "";
  const parts = [];
  if (isNumber(entry.samples)) parts.push(`${formatNumber(entry.samples, 0)} 个样本`);
  if (isNumber(entry.missing_samples) && entry.missing_samples > 0) {
    parts.push(`缺失 ${formatNumber(entry.missing_samples, 0)}`);
  }
  if (!isNumber(entry.energy_kwh)) {
    return parts.length ? `无可用能量 · ${parts.join(" · ")}` : "无可用能量数据";
  }
  return parts.join(" · ");
}

function renderCard(prefix, entry, source) {
  text(document.getElementById(`${prefix}-kwh`), isNumber(entry?.energy_kwh) ? formatFixed(entry.energy_kwh, 3) : "—");
  text(document.getElementById(`${prefix}-cost`), costLabel(entry));
  text(document.getElementById(`${prefix}-detail`), detailLabel(entry));
  const badge = document.getElementById(`${prefix}-badge`);
  if (badge) badge.replaceChildren(energyBadge(entry, source));
}

function renderSummary(payload) {
  state.summary = { ...payload, timestamp: new Date().toISOString() };
  renderCard("today", payload.today, "repository:energy_between:today");
  renderCard("yesterday", payload.yesterday, "repository:energy_between:yesterday");
  renderCard("week", payload.last_7_days, "repository:energy_between:7d");
  renderCard("month", payload.last_30_days, "repository:energy_between:30d");

  const zone = payload.timezone || "—";
  text(document.getElementById("timezone"), zone);
  text(
    document.getElementById("energy-meta"),
    `最近更新 ${formatClock(payload.timestamp)} · 时区 ${zone} · ${
      priceEnabled() ? `电价 ${formatCurrency(payload.price_per_kwh, payload.currency)} / kWh` : "电价未启用"
    }`,
  );
  renderPrice();
}

/* ------------------------------------------------------------------ live */

function renderLive(power, meter = null) {
  const quality = measurementQuality(power);
  renderMeasurement({
    valueElement: document.getElementById("live-power"),
    unitElement: document.getElementById("live-unit"),
    badgeElement: document.getElementById("live-badge"),
    measurement: power,
    digits: 1,
    scope: power?.scope,
  });

  const scope = document.getElementById("live-scope");
  if (scope) {
    scope.textContent = power?.scope || "—";
    scope.title = power?.scope || "measurement scope";
    scope.className = `pill pill--${quality === "measured" ? "ok" : "muted"}`;
  }

  let note;
  if (quality === "unavailable") {
    const missing = power?.missing_components || power?.missing || [];
    note = missing.length
      ? `不可用：缺少 ${missing.map(componentLabel).join("、")}。严格估算不会把缺失组件按 0 W 计算。`
      : `不可用：${power?.error || "没有可用的系统功率测量"}`;
  } else if (power?.scope === "wall") {
    note = `墙插实测 · ${power?.source || "external meter"} · ${formatDateTime(power?.timestamp)}`;
    if (meter?.energy_source === "meter") note += " · 电能来自电表累计值";
  } else {
    note = `DC 组件估算（不含 PSU 损耗） · ${power?.source || "calculator"} · ${formatDateTime(
      power?.timestamp,
    )}`;
  }
  if (meter?.enabled && meter.state !== "ok") {
    note += ` · 电表${meter.state === "offline" ? "离线" : meter.state === "retrying" ? "重试中" : meter.state === "starting" ? "启动中" : "异常"}，已回退组件估算`;
  }
  text(document.getElementById("live-note"), note);
}

async function refreshLive() {
  try {
    const payload = await apiGet("/api/v1/power/current");
    if (payload?.system) renderLive(payload.system, payload.meter ?? null);
  } catch {
    /* the WebSocket snapshot is the primary source */
  }
}

/* ------------------------------------------------------------------ charts */

function ensureDailyChart() {
  if (state.dailyChart) return state.dailyChart;
  const element = document.getElementById("daily-chart");
  if (!element) return null;
  state.dailyChart = mountChart(element);
  return state.dailyChart;
}

function ensureHourlyChart() {
  if (state.hourlyChart) return state.hourlyChart;
  const element = document.getElementById("hourly-chart");
  if (!element) return null;
  state.hourlyChart = mountChart(element);
  return state.hourlyChart;
}

async function loadDaily() {
  const chart = ensureDailyChart();
  if (!chart) return;
  try {
    const payload = await apiGet("/api/v1/energy/daily", { days: DAILY_DAYS });
    const days = Array.isArray(payload.days) ? payload.days : [];
    const categories = days.map((entry) => (entry.date || "").slice(5));
    const values = days.map((entry) => (isNumber(entry.energy_kwh) ? entry.energy_kwh : null));
    chart.instance.setOption(
      barChartOption({
        categories,
        values,
        unit: "kWh",
        digits: 3,
      }),
      { notMerge: true, lazyUpdate: true },
    );
    const available = values.filter((value) => isNumber(value)).length;
    text(
      document.getElementById("daily-meta"),
      `最近 ${DAILY_DAYS} 天 · ${available}/${days.length} 天有数据 · 时区 ${
        payload.timezone || state.summary?.timezone || "—"
      }`,
    );
  } catch (error) {
    text(document.getElementById("daily-meta"), `每日能耗不可用：${error.message}`);
  }
}

/* ------------------------------------------------------------------ yearly */

function ensureMonthlyCostChart() {
  if (state.monthlyCostChart) return state.monthlyCostChart;
  const element = document.getElementById("monthly-cost-chart");
  if (!element) return null;
  state.monthlyCostChart = mountChart(element);
  return state.monthlyCostChart;
}

function renderYearOptions(years, selected) {
  const select = document.getElementById("monthly-cost-year");
  if (!select) return;
  const values = Array.isArray(years) ? years.filter((year) => Number.isInteger(year)) : [];
  if (!values.includes(selected)) values.push(selected);
  values.sort((left, right) => right - left);
  select.replaceChildren(
    ...values.map((year) => {
      const option = createEl("option", "", `${year} 年`);
      option.value = String(year);
      option.selected = year === selected;
      return option;
    }),
  );
  select.value = String(selected);
}

function renderMonthlyCostRows(months) {
  const body = document.getElementById("monthly-cost-body");
  if (!body) return;
  const rows = (Array.isArray(months) ? months : []).map((entry) => {
    const row = createEl("tr");
    row.append(createEl("td", "", `${entry.month} 月`));
    row.append(createEl("td", "", isNumber(entry.energy_kwh) ? formatFixed(entry.energy_kwh, 2) : "—"));
    row.append(createEl("td", "", monthlyCostLabel(entry)));
    row.append(createEl("td", "", qualityText(entry.quality)));
    return row;
  });
  if (rows.length === 0) {
    const row = createEl("tr");
    const cell = createEl("td", "", "暂无数据");
    cell.colSpan = 4;
    row.append(cell);
    rows.push(row);
  }
  clear(body);
  body.append(...rows);
}

async function loadMonthlyCost() {
  const requestId = (state.monthlyCostRequest += 1);
  const params = state.monthlyCostYear ? { year: state.monthlyCostYear } : null;
  try {
    const chart = ensureMonthlyCostChart();
    const payload = await apiGet("/api/v1/energy/yearly", params);
    if (requestId !== state.monthlyCostRequest) return;
    state.monthlyCost = payload;
    state.monthlyCostYear = payload.year;

    const months = Array.isArray(payload.months) ? payload.months : [];
    const values = months.map((entry) =>
      priceEnabled() && isNumber(entry.cost) ? entry.cost : null,
    );
    renderYearOptions(payload.years, payload.year);
    if (chart) {
      chart.instance.setOption(
        barChartOption({
          categories: months.map((entry) => `${entry.month} 月`),
          values,
          unit: payload.currency || currency() || "CNY",
          digits: 2,
        }),
        { notMerge: true, lazyUpdate: true },
      );
    }
    renderMonthlyCostRows(months);

    const filled = values.filter((value) => isNumber(value)).length;
    text(
      document.getElementById("monthly-cost-meta"),
      `${payload.year} 年 · ${filled}/${months.length} 月有费用数据 · 时区 ${
        payload.timezone || "—"
      } · ${
        priceEnabled()
          ? `电价 ${formatCurrency(payload.price_per_kwh, payload.currency)} / kWh`
          : "电价未启用"
      }`,
    );
  } catch (error) {
    if (requestId !== state.monthlyCostRequest) return;
    text(document.getElementById("monthly-cost-meta"), `每月费用不可用：${error.message}`);
    renderMonthlyCostRows([]);
  }
}

function hourlyBuckets(points) {
  const buckets = new Map();
  for (const point of points || []) {
    if (!Array.isArray(point) || point.length < 2) continue;
    const timestamp = Number(point[0]);
    const value = point[1];
    if (!Number.isFinite(timestamp)) continue;
    const date = new Date(timestamp);
    date.setMinutes(0, 0, 0);
    const key = date.getTime();
    let bucket = buckets.get(key);
    if (!bucket) {
      bucket = { values: [], start: date };
      buckets.set(key, bucket);
    }
    if (isNumber(value)) bucket.values.push(value);
  }
  return buckets;
}

function lastHourStarts(count) {
  const starts = [];
  const cursor = new Date();
  cursor.setMinutes(0, 0, 0);
  for (let index = count - 1; index >= 0; index -= 1) {
    starts.push(new Date(cursor.getTime() - index * 3_600_000));
  }
  return starts;
}

async function loadHourly() {
  const chart = ensureHourlyChart();
  if (!chart) return;
  const badge = document.getElementById("hourly-badge");
  try {
    const payload = await fetchHistory("24h", ["system"]);
    const series = (payload.series || []).find((item) => item.id === "system");
    const buckets = hourlyBuckets(series?.points || []);
    const starts = lastHourStarts(HOURS);
    const categories = starts.map((start) => formatHourShort(start));
    const values = starts.map((start) => {
      const bucket = buckets.get(start.getTime());
      if (!bucket || bucket.values.length === 0) return null;
      return bucket.values.reduce((sum, value) => sum + value, 0) / bucket.values.length;
    });
    chart.instance.setOption(
      barChartOption({
        categories,
        values,
        unit: "W",
        digits: 1,
      }),
      { notMerge: true, lazyUpdate: true },
    );
    const filled = values.filter((value) => isNumber(value)).length;
    text(
      document.getElementById("hourly-meta"),
      `最近 ${HOURS} 小时 · ${filled}/${HOURS} 小时有数据 · 来源 ${payload.source}`,
    );
    if (badge) {
      badge.replaceChildren(
        filled === 0
          ? energyBadge({ quality: "unavailable" }, `history:${payload.source}`, "dc_components")
          : seriesBadge(series, payload),
      );
    }
  } catch (error) {
    text(document.getElementById("hourly-meta"), `每小时功耗不可用：${error.message}`);
    if (badge) badge.replaceChildren(energyBadge({ quality: "unavailable" }, "history:system"));
  }
}

/* ------------------------------------------------------------------ refresh */

async function loadSummary() {
  try {
    const payload = await apiGet("/api/v1/energy/summary");
    renderSummary(payload);
  } catch (error) {
    text(document.getElementById("energy-meta"), `能耗汇总不可用：${error.message}`);
    for (const prefix of ["today", "yesterday", "week", "month"]) {
      text(document.getElementById(`${prefix}-kwh`), "—");
      const badge = document.getElementById(`${prefix}-badge`);
      if (badge) {
        badge.replaceChildren(
          energyBadge({ quality: "unavailable" }, `repository:energy_between:${prefix}`),
        );
      }
    }
  }
}

async function refreshAll() {
  if (state.loading) return;
  state.loading = true;
  try {
    await Promise.all([loadSummary(), loadDaily(), loadHourly(), loadMonthlyCost()]);
  } finally {
    state.loading = false;
  }
}

function handleSnapshot(snapshot) {
  if (snapshot?.system?.power) renderLive(snapshot.system.power, snapshot.meter ?? null);
}

function init() {
  const yearSelect = document.getElementById("monthly-cost-year");
  if (yearSelect) {
    yearSelect.addEventListener("change", (event) => {
      const year = Number(event.target.value);
      if (!Number.isInteger(year)) return;
      state.monthlyCostYear = year;
      loadMonthlyCost();
    });
  }

  const realtime = getRealtime();
  realtime.addEventListener("snapshot", (event) => handleSnapshot(event.detail));
  realtime.addEventListener("reconnected", () => {
    refreshLive();
    refreshAll();
  });
  realtime.start();

  refreshLive();
  apiGet("/api/v1/config")
    .then((payload) => {
      state.config = payload;
      renderPrice();
    })
    .catch(() => {
      /* summary still carries price metadata */
    });
  refreshAll();

  state.refreshTimer = window.setInterval(() => {
    if (document.visibilityState === "visible") refreshAll();
  }, REFRESH_MS);

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshLive();
      refreshAll();
    }
  });
}

init();
