import {
  clear,
  createEl,
  formatClock,
  formatDateTime,
  formatFixed,
  formatPercent,
  getRealtime,
  HistoryView,
  isNumber,
  measurementValue,
  PALETTE,
  qualityBadge,
  renderMeasurement,
  text,
} from "../core.js";

const state = { view: null };

function packageStat(label, measurement, digits = 1) {
  const wrapper = createEl("div", "stat");
  wrapper.append(createEl("div", "stat__label", label));
  const value = createEl("div", "stat__value");
  const number = createEl("span", "", measurementValue(measurement) === null ? "—" : formatFixed(measurementValue(measurement), digits));
  const unit = createEl("span", "muted", measurement?.unit || "");
  value.append(number, unit, qualityBadge(measurement));
  wrapper.append(value);
  return wrapper;
}

function renderPackages(cpu) {
  const grid = document.getElementById("package-grid");
  if (!grid) return;
  grid.replaceChildren();
  if (!cpu || !Array.isArray(cpu.packages) || cpu.packages.length === 0) {
    grid.append(createEl("p", "muted small", "没有可用的 CPU package 数据"));
    return;
  }

  for (const item of cpu.packages) {
    const card = createEl("article", "package-card");
    card.dataset.quality = item.power?.quality || "unavailable";

    const head = createEl("div", "package-card__head");
    const titleBlock = createEl("div");
    titleBlock.append(
      createEl("div", "package-card__title", item.name || `package-${item.package_id}`),
      createEl(
        "div",
        "package-card__sub",
        `${item.core_count ?? "—"}C / ${item.thread_count ?? "—"}T · package ${item.package_id}`,
      ),
    );
    head.append(titleBlock, qualityBadge(item.power));
    card.append(head);

    const power = createEl("div", "package-card__power");
    const powerValue = createEl(
      "span",
      "",
      measurementValue(item.power) === null ? "—" : formatFixed(measurementValue(item.power), 1),
    );
    power.append(powerValue, createEl("span", "", item.power?.unit || "W"));
    card.append(power);

    const stats = createEl("div", "stat-grid");
    stats.append(
      packageStat("温度", item.temperature, 1),
      packageStat("频率", item.frequency_mhz, 0),
      packageStat("使用率", item.usage_percent, 1),
      packageStat("能量", item.energy_j, 1),
    );
    card.append(stats);
    grid.append(card);
  }
}

function renderCpu(snapshot) {
  const cpu = snapshot.cpu;
  const power = cpu?.total_power;
  const card = document.getElementById("cpu-card");
  if (card) card.dataset.quality = power?.quality || "unavailable";

  renderMeasurement({
    valueElement: document.getElementById("cpu-power"),
    unitElement: document.getElementById("cpu-power-unit"),
    badgeElement: document.getElementById("cpu-power-badge"),
    measurement: power,
    digits: 1,
  });
  text(
    document.getElementById("cpu-model"),
    cpu ? `${cpu.model || "CPU"} · ${cpu.socket_count ?? "—"} socket` : "CPU 尚未采集",
  );
  text(
    document.getElementById("cpu-source"),
    power ? `${power.source || "unknown"} · ${formatDateTime(power.timestamp)}` : "",
  );

  renderMeasurement({
    valueElement: document.getElementById("cpu-usage"),
    unitElement: document.getElementById("cpu-usage-unit"),
    badgeElement: document.getElementById("cpu-usage-badge"),
    measurement: cpu?.usage_percent,
    digits: 1,
    fallbackUnit: "%",
  });
  text(document.getElementById("cpu-usage-sub"), "全部核心平均");

  renderMeasurement({
    valueElement: document.getElementById("cpu-temp"),
    unitElement: document.getElementById("cpu-temp-unit"),
    badgeElement: document.getElementById("cpu-temp-badge"),
    measurement: cpu?.max_temperature,
    digits: 1,
    fallbackUnit: "°C",
  });
  text(document.getElementById("cpu-temp-sub"), "hwmon coretemp / k10temp");

  text(document.getElementById("cpu-sockets"), cpu?.socket_count ?? "—");
  if (cpu?.packages?.length) {
    const cores = cpu.packages.reduce((sum, item) => sum + (item.core_count || 0), 0);
    const threads = cpu.packages.reduce((sum, item) => sum + (item.thread_count || 0), 0);
    text(document.getElementById("cpu-cores"), `${cores}C / ${threads}T · ${cpu.packages.length} package`);
  } else {
    text(document.getElementById("cpu-cores"), "—");
  }
  text(document.getElementById("cpu-timestamp"), formatClock(snapshot.timestamp));

  const alert = document.getElementById("cpu-alert");
  if (alert) {
    const usage = measurementValue(power);
    if (usage === null) {
      alert.hidden = false;
      alert.className = "alert";
      alert.textContent = `CPU 功率不可用：${power?.error || "RAPL 未返回数据"}。不使用 0 W 代替。`;
    } else {
      alert.hidden = true;
      alert.textContent = "";
    }
  }

  const note = document.getElementById("cpu-note");
  if (note) {
    const health = snapshot.health?.cpu;
    text(
      note,
      health?.last_error
        ? `采集器状态：${health.state} · ${health.last_error}`
        : "RAPL 计数器回绕已在服务端处理；无 DRAM 域时不作声明。",
    );
  }

  renderPackages(cpu);
}

function handleSnapshot(snapshot) {
  renderCpu(snapshot);
  if (state.view) state.view.append(snapshot);
}

function init() {
  const view = new HistoryView({
    element: document.getElementById("cpu-chart"),
    rangeGroup: document.querySelector('[data-range-group="cpu"]'),
    definitions: [
      {
        id: "cpu",
        label: "CPU",
        color: PALETTE.cpu,
        extract: (snapshot) => snapshot.cpu?.total_power,
      },
    ],
    window: "5m",
    metaElement: document.getElementById("cpu-chart-meta"),
  });
  state.view = view;
  view.load("5m");

  const realtime = getRealtime();
  realtime.addEventListener("snapshot", (event) => handleSnapshot(event.detail));
  realtime.addEventListener("reconnected", () => view.load(view.window));
  realtime.start();

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible" && view.window !== "5m") view.refresh();
  });
}

init();
