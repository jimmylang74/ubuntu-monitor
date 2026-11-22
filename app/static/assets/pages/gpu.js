import {
  apiGet,
  createEl,
  formatBytes,
  formatDateTime,
  formatFixed,
  formatNumber,
  getRealtime,
  gpuTotalMeasurement,
  HistoryView,
  isNumber,
  measurementValue,
  PALETTE,
  qualityBadge,
  text,
} from "../core.js";

const state = {
  view: null,
  gpus: [],
  gpuKey: "",
  timer: null,
};

function definitions() {
  const items = [
    {
      id: "gpu_total",
      label: "GPU Total",
      color: PALETTE.gpuTotal,
      extract: gpuTotalMeasurement,
    },
  ];
  state.gpus.forEach((gpu, index) => {
    items.push({
      id: `gpu:${gpu.uuid}`,
      label: `GPU ${index}`,
      color: PALETTE.gpu[index % PALETTE.gpu.length],
      extract: (snapshot) => (snapshot.gpu || []).find((item) => item.uuid === gpu.uuid)?.power,
    });
  });
  return items;
}

function applyGpus(gpus) {
  const sorted = [...(gpus || [])].sort((a, b) => (a.index ?? 0) - (b.index ?? 0));
  const key = sorted.map((gpu) => gpu.uuid).join("|");
  if (key === state.gpuKey) return;
  state.gpuKey = key;
  state.gpus = sorted;
  if (state.view) state.view.setDefinitions(definitions());
}

async function refreshGpuList() {
  try {
    const payload = await apiGet("/api/v1/gpu");
    applyGpus(payload.gpus || []);
  } catch {
    /* the WebSocket snapshot still carries the GPU list */
  }
}

function stat(label, measurement, formatter, fallbackUnit = "") {
  const wrapper = createEl("div", "stat");
  wrapper.append(createEl("div", "stat__label", label));
  const value = createEl("div", "stat__value");
  const number = createEl(
    "span",
    "",
    measurementValue(measurement) === null ? "—" : formatter(measurementValue(measurement)),
  );
  value.append(number, createEl("span", "muted", measurement?.unit || fallbackUnit), qualityBadge(measurement));
  wrapper.append(value);
  return wrapper;
}

function renderGpuCards(snapshot) {
  const grid = document.getElementById("gpu-grid");
  if (!grid) return;
  grid.replaceChildren();

  const gpus = snapshot.gpu || [];
  if (!snapshot.gpu_available) {
    grid.append(createEl("p", "muted", "GPU 枚举失败，暂无设备卡片。"));
    return;
  }
  if (gpus.length === 0) {
    grid.append(createEl("p", "muted", "系统可用，但没有检测到 NVIDIA GPU。"));
    return;
  }

  for (const gpu of gpus) {
    const card = createEl("article", "gpu-card");
    card.dataset.available = "true";

    const head = createEl("div", "gpu-card__head");
    const title = createEl("div");
    title.append(
      createEl("div", "gpu-card__title", `${gpu.index ?? "?"} · ${gpu.name || "NVIDIA GPU"}`),
      createEl("div", "gpu-card__sub", `${gpu.uuid} · PCI ${gpu.pci_bus_id || "—"}`),
    );
    head.append(title, qualityBadge(gpu.power));
    card.append(head);

    const power = createEl("div", "gpu-card__power");
    power.append(
      createEl(
        "span",
        "",
        measurementValue(gpu.power) === null ? "—" : formatFixed(measurementValue(gpu.power), 1),
      ),
      createEl("span", "", gpu.power?.unit || "W"),
    );
    card.append(power);

    const stats = createEl("div", "stat-grid");
    stats.append(
      stat("功耗上限", gpu.power_limit, (value) => formatFixed(value, 0), "W"),
      stat("温度", gpu.temperature, (value) => formatFixed(value, 0), "°C"),
      stat("利用率", gpu.utilization, (value) => formatNumber(value, 0), "%"),
      stat("风扇", gpu.fan_speed, (value) => formatFixed(value, 0), "%"),
      stat("核心时钟", gpu.core_clock, (value) => formatFixed(value, 0), "MHz"),
      stat("显存时钟", gpu.memory_clock, (value) => formatFixed(value, 0), "MHz"),
    );

    const memory = createEl("div", "stat");
    memory.append(createEl("div", "stat__label", "显存已用 / 总量"));
    const memoryValue = createEl("div", "stat__value");
    memoryValue.append(
      createEl(
        "span",
        "",
        `${formatBytes(measurementValue(gpu.memory_used))} / ${formatBytes(
          measurementValue(gpu.memory_total),
        )}`,
      ),
      qualityBadge(gpu.memory_used),
    );
    memory.append(memoryValue);
    stats.append(memory);

    card.append(stats);
    grid.append(card);
  }
}

function renderGpu(snapshot) {
  const gpus = snapshot.gpu || [];
  const total = gpuTotalMeasurement(snapshot);
  const available = document.getElementById("gpu-available");
  if (available) {
    available.textContent = snapshot.gpu_available ? "GPU 可用" : "GPU 不可用";
    available.className = `pill pill--${snapshot.gpu_available ? "ok" : "error"}`;
  }

  const driver = document.getElementById("gpu-driver");
  if (driver) {
    const source = gpus[0]?.power?.source;
    driver.textContent = source ? String(source).split(":")[0] : "NVML";
    driver.title = source || "NVIDIA Management Library";
  }

  if (!snapshot.gpu_available) {
    text(document.getElementById("gpu-summary"), `GPU 枚举失败：${snapshot.gpu_error || "NVML 不可用"}`);
  } else if (gpus.length === 0) {
    text(document.getElementById("gpu-summary"), "系统可用，但未检测到 NVIDIA GPU（GPU 总功耗按 0 W measured 记录）。");
  } else {
    text(
      document.getElementById("gpu-summary"),
      `${gpus.length} 张 GPU · 合计 ${formatFixed(measurementValue(total), 1)} W · 最近更新 ${formatDateTime(
        snapshot.timestamp,
      )}`,
    );
  }

  const alert = document.getElementById("gpu-alert");
  if (alert) {
    if (!snapshot.gpu_available) {
      alert.hidden = false;
      alert.className = "alert alert--error";
      alert.textContent = `GPU 不可用：${snapshot.gpu_error || "枚举失败"}。系统功率将按严格估算规则显示为不可用；不会把 GPU 当作 0 W。`;
    } else if (gpus.length === 0) {
      alert.hidden = false;
      alert.className = "alert";
      alert.textContent = "没有检测到 GPU。这不是驱动错误，GPU 总功耗为 0 W measured。";
    } else {
      alert.hidden = true;
      alert.textContent = "";
    }
  }

  renderGpuCards(snapshot);
}

function handleSnapshot(snapshot) {
  renderGpu(snapshot);
  applyGpus(snapshot.gpu || []);
  if (state.view) state.view.append(snapshot);
}

function init() {
  const view = new HistoryView({
    element: document.getElementById("gpu-chart"),
    rangeGroup: document.querySelector('[data-range-group="gpu"]'),
    definitions: definitions(),
    window: "5m",
    metaElement: document.getElementById("gpu-chart-meta"),
  });
  state.view = view;
  view.load("5m");

  const realtime = getRealtime();
  realtime.addEventListener("snapshot", (event) => handleSnapshot(event.detail));
  realtime.addEventListener("reconnected", () => view.load(view.window));
  realtime.start();

  refreshGpuList();
  state.timer = window.setInterval(refreshGpuList, 30_000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshGpuList();
      if (view.window !== "5m") view.refresh();
    }
  });
}

init();
