import {
  apiGet,
  clear,
  collectorStateVariant,
  createEl,
  formatBytes,
  formatDateTime,
  formatDuration,
  formatFixed,
  formatNumber,
  formatPercent,
  formatSpeed,
  getRealtime,
  gpuTotalMeasurement,
  HistoryView,
  isNumber,
  measurementQuality,
  measurementValue,
  modeLabel,
  PALETTE,
  qualityBadge,
  renderMeasurement,
  text,
} from "../core.js";

const state = {
  view: null,
  snapshot: null,
  gpus: [],
  gpuKey: "",
  gpuTimer: null,
};

const componentLabel = (name) => {
  if (name === "cpu") return "CPU";
  if (name === "gpu") return "GPU";
  if (name.startsWith("gpu:")) return `GPU ${name.slice(4, 12)}`;
  return name;
};

function definitions() {
  const items = [
    {
      id: "system",
      label: "System",
      color: PALETTE.system,
      extract: (snapshot) => snapshot.system?.power,
    },
    {
      id: "cpu",
      label: "CPU",
      color: PALETTE.cpu,
      extract: (snapshot) => snapshot.cpu?.total_power,
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
  items.push({
    id: "gpu_total",
    label: "GPU Total",
    color: PALETTE.gpuTotal,
    extract: gpuTotalMeasurement,
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

function renderHero(snapshot) {
  const power = snapshot.system?.power;
  const quality = measurementQuality(power);
  const mode = power?.mode || quality;
  const card = document.getElementById("system-card");
  if (card) card.dataset.quality = quality;

  renderMeasurement({
    valueElement: document.getElementById("sys-value"),
    unitElement: document.getElementById("sys-unit"),
    badgeElement: document.getElementById("sys-badge"),
    measurement: power,
    digits: 1,
    scope: power?.scope,
  });

  const modeElement = document.getElementById("sys-mode");
  if (modeElement) {
    modeElement.textContent = modeLabel(mode);
    modeElement.className = `pill pill--${mode === "measured" ? "ok" : mode === "estimated" ? "warn" : "muted"}`;
  }

  const missing = power?.missing_components || power?.missing || [];
  let note;
  if (quality === "unavailable") {
    const reason = missing.length
      ? `缺少：${missing.map(componentLabel).join("、")}`
      : power?.error || "没有可用的组件测量";
    note = `系统功率不可用。${reason}。严格估算模式不会把缺失组件按 0 W 计算。`;
  } else if (power?.scope === "wall") {
    note = "墙插实测功率，来自外部电表。";
  } else {
    note = `DC 组件估算：CPU + GPU 实测值 + 其他部件 ${formatFixed(
      snapshot.system?.other_components_w,
      0,
    )} W 常量，不含 PSU 转换损耗。`;
  }
  text(document.getElementById("sys-note"), note);
  text(
    document.getElementById("sys-source"),
    power ? `${power.source || "unknown"} · ${formatDateTime(power.timestamp)}` : "",
  );
}

function renderMetrics(snapshot) {
  const cpu = snapshot.cpu;
  renderMeasurement({
    valueElement: document.getElementById("cpu-value"),
    unitElement: document.getElementById("cpu-unit"),
    badgeElement: document.getElementById("cpu-badge"),
    measurement: cpu?.total_power,
  });
  if (cpu) {
    const packageCount = cpu.packages?.length ?? 0;
    text(
      document.getElementById("cpu-sub"),
      `${formatPercent(measurementValue(cpu.usage_percent))} 使用率 · ${formatFixed(
        measurementValue(cpu.max_temperature),
        0,
      )} °C · ${packageCount} package`,
    );
  } else {
    text(document.getElementById("cpu-sub"), "CPU 尚未采集");
  }

  const gpuMeasurement = gpuTotalMeasurement(snapshot);
  renderMeasurement({
    valueElement: document.getElementById("gpu-value"),
    unitElement: document.getElementById("gpu-unit"),
    badgeElement: document.getElementById("gpu-badge"),
    measurement: gpuMeasurement,
  });
  if (!snapshot.gpu_available) {
    text(document.getElementById("gpu-sub"), `枚举失败：${snapshot.gpu_error || "NVML 不可用"}`);
  } else {
    const gpus = snapshot.gpu || [];
    text(
      document.getElementById("gpu-sub"),
      gpus.length ? `${gpus.length} 张卡 · ${gpus.map((gpu) => gpu.name).join(" / ")}` : "未检测到 GPU（0 W）",
    );
  }

  const otherValue = snapshot.system?.other_components_w;
  const otherMeasurement = {
    value: isNumber(otherValue) ? otherValue : null,
    unit: "W",
    quality: isNumber(otherValue) ? "estimated" : "unavailable",
    source: "config:estimated_other_power_w",
    timestamp: snapshot.timestamp,
    error: isNumber(otherValue) ? null : "estimated_other_power_w is not configured",
    missing: [],
  };
  renderMeasurement({
    valueElement: document.getElementById("other-value"),
    unitElement: document.getElementById("other-unit"),
    badgeElement: document.getElementById("other-badge"),
    measurement: otherMeasurement,
    digits: 0,
  });
  text(document.getElementById("other-sub"), "配置常量，不含 PSU 损耗");

  const energy = snapshot.system?.energy;
  renderMeasurement({
    valueElement: document.getElementById("energy-value"),
    unitElement: document.getElementById("energy-unit"),
    badgeElement: document.getElementById("energy-badge"),
    measurement: energy,
    digits: 3,
    fallbackUnit: "Wh",
  });
  if (measurementValue(energy) === null) {
    text(
      document.getElementById("energy-sub"),
      energy?.error || "需要两个样本才能计算区间电能",
    );
  } else {
    text(document.getElementById("energy-sub"), "自上次采样以来的积分");
  }
}

function addKv(list, label, value) {
  const dt = createEl("dt", "", label);
  const dd = createEl("dd");
  if (value instanceof Node) {
    dd.append(value);
  } else {
    dd.textContent = value === null || value === undefined ? "—" : String(value);
  }
  list.append(dt, dd);
}

const METER_STATE_LABEL = {
  disabled: "未启用",
  starting: "启动中",
  ok: "正常",
  retrying: "重试中",
  offline: "离线",
  error: "错误",
};

function meterStateLabel(value) {
  return METER_STATE_LABEL[value] || value || "unknown";
}

function statePill(value) {
  const normalized = String(value || "unknown");
  const variant =
    normalized === "ok" || normalized === "measured"
      ? "ok"
      : normalized === "degraded" ||
          normalized === "estimated" ||
          normalized === "stale" ||
          normalized === "retrying" ||
          normalized === "starting"
        ? "warn"
        : normalized === "error" || normalized === "offline"
          ? "error"
          : "muted";
  return createEl("span", `pill pill--${variant}`, normalized);
}

function measurementCell(measurement, digits = 1) {
  const wrap = createEl("span", "meter-cell");
  const value = measurementValue(measurement);
  wrap.append(
    document.createTextNode(
      value === null ? "—" : `${formatFixed(value, digits)} ${measurement.unit || ""}`.trim(),
    ),
  );
  if (measurement) wrap.append(qualityBadge(measurement, { scope: measurement.scope || "wall" }));
  return wrap;
}

function renderStatus(snapshot) {
  const host = snapshot.host || {};
  const database = snapshot.database || {};
  const list = document.getElementById("status-list");
  if (!list) return;
  list.replaceChildren();

  addKv(list, "主机", `${host.hostname || "—"}${host.kernel ? ` · ${host.kernel}` : ""}`);
  addKv(list, "系统运行", formatDuration(Number(host.uptime_s)));
  addKv(
    list,
    "服务运行",
    snapshot.started_at
      ? formatDuration((Date.now() - new Date(snapshot.started_at).getTime()) / 1000)
      : "—",
  );
  addKv(
    list,
    "GPU",
    snapshot.gpu_available
      ? `${(snapshot.gpu || []).length} 张卡 · 可用`
      : `不可用 · ${snapshot.gpu_error || "枚举失败"}`,
  );

  const meter = snapshot.meter;
  if (meter) {
    addKv(
      list,
      "外部电表",
      `${meter.type || "—"} · ${meterStateLabel(meter.state)}${meter.healthy ? " · wall" : ""}`,
    );
    if (meter.enabled) {
      const reading = meter.reading || {};
      addKv(list, "电表功率", measurementCell(reading.power));
      addKv(
        list,
        "累计电能",
        reading.energy_total
          ? measurementCell(reading.energy_total, 3)
          : "—",
      );
      addKv(
        list,
        "电能来源",
        meter.energy_source === "meter" ? "电表累计寄存器" : "功率积分",
      );
      addKv(list, "最后成功", formatDateTime(meter.last_success));
      addKv(
        list,
        "连续失败",
        `${meter.consecutive_failures ?? 0} / ${meter.failover_after ?? "—"}`,
      );
      if (meter.last_error) {
        const row = createEl("span", "muted small", meter.last_error);
        addKv(list, "电表错误", row);
      }
    }
  }
  addKv(
    list,
    "数据库",
    `${database.state || "unknown"} · 已写 ${database.written ?? "—"} · 丢弃 ${database.dropped ?? "—"}`,
  );
  addKv(
    list,
    "写队列",
    `${database.queued ?? "—"} / ${database.capacity ?? "—"}${
      database.failed ? ` · 失败 ${database.failed}` : ""
    }`,
  );
  addKv(list, "采样时间", formatDateTime(snapshot.timestamp));

  const dbState = document.getElementById("db-state");
  if (dbState) {
    const value = database.state || "unknown";
    dbState.replaceChildren(statePill(value));
    dbState.className = "";
  }

  const chips = document.getElementById("health-chips");
  if (chips) {
    chips.replaceChildren();
    const health = snapshot.health || {};
    for (const [name, item] of Object.entries(health)) {
      const stateValue = item?.state || "unknown";
      const chip = createEl(
        "span",
        `chip chip--${collectorStateVariant(stateValue)}`,
        `${name}: ${stateValue}`,
      );
      if (item?.last_error) chip.title = item.last_error;
      chips.append(chip);
    }
  }

  const alert = document.getElementById("gpu-alert");
  if (alert) {
    if (snapshot.gpu_available) {
      alert.hidden = true;
      alert.textContent = "";
    } else {
      alert.hidden = false;
      alert.className = "alert alert--error";
      alert.textContent = `GPU 不可用：${snapshot.gpu_error || "枚举失败"}。严格估算模式下整机功率显示为不可用。`;
    }
  }
}

function renderMeter(snapshot) {
  const meter = snapshot.meter;
  const alert = document.getElementById("meter-alert");
  if (!alert) return;
  if (!meter) {
    alert.hidden = true;
    alert.textContent = "";
    return;
  }
  if (!meter.enabled) {
    alert.hidden = true;
    alert.textContent = "";
    return;
  }
  if (meter.state === "offline" || meter.state === "error") {
    alert.hidden = false;
    alert.className = "alert alert--error";
    alert.textContent = `外部电表${meterStateLabel(meter.state)}（连续失败 ${
      meter.consecutive_failures ?? 0
    }/${meter.failover_after ?? "—"}）：${
      meter.last_error || "无法读取"
    }。整机功率已退回 DC 组件估算。`;
  } else if (meter.state === "retrying" || meter.state === "starting") {
    alert.hidden = false;
    alert.className = "alert";
    alert.textContent = `外部电表${meterStateLabel(meter.state)}，连续失败 ${
      meter.consecutive_failures ?? 0
    }/${meter.failover_after ?? "—"}；恢复前使用组件估算。`;
  } else {
    alert.hidden = true;
    alert.textContent = "";
  }
}

function renderMemory(memory) {
  const percent = measurementValue(memory?.usage_percent);
  text(document.getElementById("mem-percent"), percent === null ? "—" : formatNumber(percent, 1));
  const bar = document.getElementById("mem-bar");
  if (bar) {
    const width = isNumber(percent) ? Math.max(0, Math.min(100, percent)) : 0;
    bar.style.width = `${width}%`;
  }
  text(document.getElementById("mem-used"), memory ? formatBytes(measurementValue(memory.used)) : "—");
  text(document.getElementById("mem-total"), memory ? formatBytes(measurementValue(memory.total)) : "—");
  text(
    document.getElementById("mem-available"),
    memory ? formatBytes(measurementValue(memory.available)) : "—",
  );
  text(
    document.getElementById("swap-used"),
    memory
      ? `${formatBytes(measurementValue(memory.swap_used))} / ${formatBytes(
          measurementValue(memory.swap_total),
        )}`
      : "—",
  );
  const badge = document.getElementById("memory-badge");
  if (badge) {
    badge.replaceChildren(qualityBadge(memory?.usage_percent));
  }
}

function renderDisks(disks) {
  const body = document.getElementById("disks-body");
  if (!body) return;
  body.replaceChildren();
  if (!Array.isArray(disks) || disks.length === 0) {
    body.append(createEl("p", "muted small", "尚未采集磁盘信息"));
    return;
  }
  for (const disk of disks) {
    const percent = measurementValue(disk.usage_percent);
    const row = createEl("div", "list-row");
    const title = createEl("div", "list-row__title", disk.mountpoint || disk.device || "disk");
    const value = createEl("div", "list-row__value", formatPercent(percent));
    const sub = createEl(
      "div",
      "list-row__sub",
      `${formatBytes(measurementValue(disk.used))} / ${formatBytes(
        measurementValue(disk.total),
      )}${disk.device ? ` · ${disk.device}` : ""}`,
    );
    const bar = createEl("div", "bar");
    const fill = createEl("span");
    if (isNumber(percent)) fill.style.width = `${Math.max(0, Math.min(100, percent))}%`;
    bar.append(fill);
    row.append(title, value, sub, bar);
    body.append(row);
  }
}

function renderNetwork(interfaces) {
  const body = document.getElementById("network-body");
  if (!body) return;
  body.replaceChildren();
  if (!Array.isArray(interfaces) || interfaces.length === 0) {
    body.append(createEl("p", "muted small", "尚未采集网络信息"));
    return;
  }
  for (const iface of interfaces) {
    const row = createEl("div", "list-row");
    const title = createEl("div", "list-row__title", iface.name || "interface");
    const value = createEl(
      "div",
      "list-row__value",
      `↓ ${formatSpeed(measurementValue(iface.rx_mbps))} · ↑ ${formatSpeed(
        measurementValue(iface.tx_mbps),
      )}`,
    );
    const sub = createEl(
      "div",
      "list-row__sub",
      `累计 RX ${formatBytes(measurementValue(iface.rx_total))} · TX ${formatBytes(
        measurementValue(iface.tx_total),
      )}`,
    );
    row.append(title, value, sub);
    body.append(row);
  }
}

function handleSnapshot(snapshot) {
  state.snapshot = snapshot;
  renderHero(snapshot);
  renderMetrics(snapshot);
  renderStatus(snapshot);
  renderMeter(snapshot);
  renderMemory(snapshot.memory);
  renderDisks(snapshot.disks);
  renderNetwork(snapshot.network);
  applyGpus(snapshot.gpu || []);
  if (state.view) state.view.append(snapshot);
}

function init() {
  const view = new HistoryView({
    element: document.getElementById("power-chart"),
    rangeGroup: document.querySelector('[data-range-group="main"]'),
    definitions: definitions(),
    window: "5m",
    metaElement: document.getElementById("chart-meta"),
  });
  state.view = view;
  view.load("5m");

  const realtime = getRealtime();
  realtime.addEventListener("snapshot", (event) => handleSnapshot(event.detail));
  realtime.addEventListener("reconnected", () => view.load(view.window));
  realtime.start();

  refreshGpuList();
  state.gpuTimer = window.setInterval(refreshGpuList, 30_000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      refreshGpuList();
      if (state.view && state.view.window !== "5m") state.view.refresh();
    }
  });
}

init();
