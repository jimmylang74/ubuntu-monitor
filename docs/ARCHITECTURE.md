# 架构设计

- 版本：v0.5（Phase 5 产出）
- 需求基准：Ubuntu Power Monitor PRD
- 目标：在不编造数据的前提下，提供 1 秒级 CPU / GPU / 整机功耗与历史能耗。

## 1. 设计原则

1. **采集与请求解耦**：浏览器请求只读内存状态或 SQLite，绝不触发硬件采集。
2. **来源可追溯**：每个功耗值都带 `value / unit / quality / source / timestamp`。
3. **不可用即 `null`**：`unavailable` 永远不等于 0，不用 0 W 冒充缺失数据。
4. **估算与实测严格区分**：只有外部电表能产生 `measured` 的整机功耗；组件求和永远是 `estimated`。
5. **故障隔离**：单个采集器、单张 GPU、单个电表失败，不影响其他模块与服务进程。
6. **父子能量域不重复累加**：只对顶层 `package-*` 求和，子域仅作诊断展示。
7. **适配器隔离**：外部电表只实现 `PowerMeterAdapter`，不接触 CPU/GPU 采集代码。
8. **边界有界**：内存状态、WebSocket 队列、前端曲线缓冲区、数据库写入队列全部有上限。
9. **可测试**：所有硬件读取都可通过注入时钟 / 临时 sysfs 目录 / 假 NVML 替换。

## 2. 运行时拓扑

```
                       Ubuntu Server
                            │
                 ┌──────────┴──────────┐
                 │  Collector Service  │  asyncio，独立周期任务
                 └──────────┬──────────┘
        ┌───────────────┬───┴────────┬────────────────┐
        │               │            │                │
   RAPL/powercap   NVML / nvidia-smi  psutil      PowerMeter
   （每 package）   （每 GPU, UUID）   mem/disk/net  （可选, 实测）
        │               │            │                │
        └───────────────┴─────┬──────┴────────────────┘
                              │
                     Data Aggregator
                 （快照 + 系统功耗计算）
                              │
                 ┌────────────┴────────────┐
                 │                         │
          Current State               SQLite (WAL)
          （内存，最新值）             raw / 1m / 1h
                 │                         │
                 └────────────┬────────────┘
                              │
                         FastAPI / WS
                              │
                        Web Browser
```

进程模型：**单进程 + asyncio 任务**（不引入 Celery/Redis）。硬件读取是短小的同步调用，
通过 `asyncio.to_thread()` 执行并加超时与“单飞”保护，避免阻塞事件循环。

## 3. 代码结构与依赖

```
power-monitor/
├── app/
│   ├── main.py                 # FastAPI 应用装配、lifespan
│   ├── config.py               # YAML + 环境变量 → 强类型配置
│   ├── domain.py               # 数据模型（无外部依赖）
│   ├── state.py                # StateStore + EventBus
│   ├── cli.py                  # check / doctor / sample 命令
│   ├── collectors/
│   │   ├── base.py             # Collector 协议、周期调度、健康状态
│   │   ├── cpu.py              # RAPL + topology + hwmon + psutil
│   │   ├── gpu.py              # NVML/nvidia-smi 封装、按 UUID 聚合
│   │   ├── memory.py
│   │   ├── disk.py
│   │   ├── network.py
│   │   └── system.py           # 主机信息、运行时长
│   ├── power/
│   │   ├── base.py             # 采样/能耗积分公共逻辑
│   │   ├── calculator.py       # 实测/估算决策
│   │   └── adapters/
│   │       ├── rapl.py         # powercap 读取、回绕、多 package
│   │       ├── nvidia.py       # NVML / nvidia-smi
│   │       ├── base.py         # PowerMeterAdapter 契约/工厂
│   │       ├── shelly.py       # Shelly Gen1/Gen2
│   │       ├── modbus.py       # Modbus TCP
│   │       └── http_pdu.py     # 通用 JSON-over-HTTP（只读）
│   ├── api/
│   │   ├── routes.py           # REST
│   │   └── websocket.py        # /ws/realtime
│   ├── services/
│   │   ├── collector_service.py
│   │   ├── meter_service.py    # 电表轮询/切换/恢复/状态
│   │   ├── statistics_service.py
│   │   ├── retention_service.py
│   │   ├── history_service.py
│   │   └── energy_service.py
│   ├── database/
│   │   ├── models.py           # SQLAlchemy 2.0
│   │   └── repository.py       # 所有 SQL 只出现在这里
│   ├── web.py                  # Jinja2 页面、静态资源挂载
│   ├── templates/
│   │   ├── base.html
│   │   ├── index.html
│   │   ├── cpu.html
│   │   ├── gpu.html
│   │   └── energy.html
│   └── static/
│       ├── assets/             # app.css / core.js / pages/*.js
│       └── vendor/echarts.min.js
├── config/config.example.yaml
├── data/                       # SQLite（运行时生成）
├── backups/                    # 备份输出（运行时生成）
├── docs/
├── deploy/power-monitor.service
├── scripts/
│   ├── run.sh                  # 唯一启动入口
│   ├── install-service.sh
│   ├── install-rapl-helper.sh
│   ├── backup.py / backup.sh
│   └── stability_check.py
├── tests/
├── requirements.txt
├── pyproject.toml
├── run.py
└── README.md
```

依赖方向（单向，禁止反向 import）：

```
config ──────────────────────────────┐
domain（零依赖）──────────────────────┤
collectors ──> domain, config        │
power/adapters ──> domain, config    ├──> services ──> api ──> main
power/calculator ──> domain          │        │
database/repository ──> domain, db   │        └──> state
state（零依赖）───────────────────────┘
```

硬性规则：

- `collectors/*` 不得 import FastAPI、SQLAlchemy、`services`。
- `api/*` 不得 import 任何采集器；只能读 `StateStore` / `repository`。
- 外部电表适配器不得 import `collectors.cpu` / `collectors.gpu`。
- 只有 `repository.py` 直接写 SQL。

## 4. 核心数据模型（`app/domain.py`）

Python 3.12，`dataclass(frozen=True, slots=True)` + 泛型 `Measurement[T]`。

```python
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Generic, Literal, TypeVar

class Quality(StrEnum):
    MEASURED = "measured"
    ESTIMATED = "estimated"
    UNAVAILABLE = "unavailable"

@dataclass(frozen=True, slots=True)
class Measurement(Generic[T]):
    value: T | None            # None ⇔ quality == UNAVAILABLE
    unit: str                  # "W" | "J" | "°C" | "%" | "MHz" | "bytes" | "MB/s" | "kWh" | "V" | "A"
    quality: Quality
    source: str                # "rapl:/sys/class/powercap/intel-rapl:0"
    timestamp: datetime        # aware, UTC
    error: str | None = None   # 失败原因，仅诊断用
    missing: tuple[str, ...] = ()  # 聚合值缺失的输入，如 ("package-1",)
```

```python
@dataclass(frozen=True, slots=True)
class CpuPackage:
    package_id: int
    name: str                       # "package-0"
    power: Measurement[float]       # W, RAPL 差值
    energy_j: Measurement[float]    # J, 采集启动以来累计
    temperature: Measurement[float] # °C, coretemp "Package id N"
    frequency_mhz: Measurement[float]
    usage_percent: Measurement[float]
    core_count: int
    thread_count: int

@dataclass(frozen=True, slots=True)
class CpuSnapshot:
    timestamp: datetime
    model: str
    socket_count: int
    packages: tuple[CpuPackage, ...]
    total_power: Measurement[float]
    usage_percent: Measurement[float]
    max_temperature: Measurement[float]   # 所有 package 温度最大值

@dataclass(frozen=True, slots=True)
class GpuSnapshot:
    uuid: str                       # 稳定主键，不依赖 index
    index: int                      # 仅用于展示
    name: str
    pci_bus_id: str | None
    power: Measurement[float]
    power_limit: Measurement[float]
    temperature: Measurement[float]
    utilization: Measurement[float]
    memory_used: Measurement[int]       # bytes
    memory_total: Measurement[int]
    fan_speed: Measurement[float]       # %, Tesla 常为 unavailable
    core_clock: Measurement[float]      # MHz
    memory_clock: Measurement[float]    # MHz

@dataclass(frozen=True, slots=True)
class MemorySnapshot:
    timestamp: datetime
    total: Measurement[int]
    used: Measurement[int]
    available: Measurement[int]
    usage_percent: Measurement[float]
    swap_used: Measurement[int]
    swap_total: Measurement[int]

@dataclass(frozen=True, slots=True)
class DiskUsage:
    device: str
    mountpoint: str
    total: Measurement[int]
    used: Measurement[int]
    free: Measurement[int]
    usage_percent: Measurement[float]
    read_mbps: Measurement[float]
    write_mbps: Measurement[float]
    read_iops: Measurement[float]
    write_iops: Measurement[float]

@dataclass(frozen=True, slots=True)
class NetworkInterface:
    name: str
    rx_mbps: Measurement[float]
    tx_mbps: Measurement[float]
    rx_total: Measurement[int]
    tx_total: Measurement[int]

@dataclass(frozen=True, slots=True)
class SystemPower:
    power: Measurement[float]
    mode: Literal["measured", "estimated", "unavailable"]
    other_components: float
    missing_components: tuple[str, ...]
    meter_status: str | None      # "ok" | "offline" | "disabled"
    last_meter_at: datetime | None
    scope: Literal["wall", "dc_components"]  # 电表=wall；估算=dc_components
```

```python
@dataclass(frozen=True, slots=True)
class CollectorHealth:
    name: str
    state: Literal["ok", "degraded", "error"]
    last_success: datetime | None
    last_error: str | None
    consecutive_failures: int

@dataclass(frozen=True, slots=True)
class Snapshot:
    timestamp: datetime
    cpu: CpuSnapshot
    gpus: tuple[GpuSnapshot, ...]
    memory: MemorySnapshot
    disks: tuple[DiskUsage, ...]
    networks: tuple[NetworkInterface, ...]
    system: SystemPower
    energy_total_kwh: Measurement[float]   # 进程启动以来（MVP），或电表累计值
    host: dict[str, str]
    health: dict[str, CollectorHealth]
```

### 4.1 质量语义（强约束）

| quality | 含义 | 允许的来源 |
|---|---|---|
| `measured` | 直接读取硬件计数器/温度/电表 | RAPL、NVML、psutil、PowerMeter |
| `estimated` | 实测组件求和 + 自动建议值或配置常量 | `calculator:components` |
| `unavailable` | 无法得出，`value=None` | 任何失败路径 |

聚合规则：

- CPU 总功耗 = 所有 package 功耗之和；**只有全部 package 都是 `measured` 才允许输出 `measured`**，
  否则输出 `unavailable`，并在 `missing` 中列出缺失的 package。
- GPU 总功耗同理；单张 GPU 失败不影响其他 GPU 的独立卡片，但 GPU 总和与整机估算变为不可用。
- 未检测到任何 GPU 时，GPU 总功耗 = `0 W measured`，`source="gpu:none-in-total"`（这是“确实没有”，不是缺失）。
- 估算整机功耗 = CPU 总和 + 计入求和的 GPU 总和 + `other_components_w`；`quality=estimated`、`scope=dc_components`，
  `other_power_source` 指明该值是自动建议还是配置常量。
- 外部电表 = `quality=measured`、`scope=wall`。
- **估算值永远不能标记为 `measured`，也不能与电表值混用。**

## 5. 采集层

### 5.1 调度器（`collectors/base.py`）

```python
class Collector(Protocol):
    name: str
    interval_s: float
    def collect(self) -> object: ...      # 同步、短耗时、可抛异常
```

```python
class PeriodicTask:
    async def run(self) -> None:
        while not self._stop.is_set():
            if self._inflight:              # 上一次仍未返回 → 跳过本轮
                await self._sleep(self.interval_s)
                continue
            self._inflight = True
            t0 = time.monotonic()
            try:
                result = await asyncio.wait_for(
                    asyncio.to_thread(self.collector.collect),
                    timeout=self.timeout_s,
                )
                self.store.update(self.collector.name, result)
                self.health.mark_ok()
            except Exception as exc:
                self.health.mark_error(exc)  # 记日志、保留上一次可用值并标记 stale
            finally:
                self._inflight = False
                await self._sleep(max(0.0, self.interval_s - (time.monotonic() - t0)))
```

关键点：

- `to_thread` 无法真正取消，因此用 `_inflight` 防止卡死时无限堆积线程。
- 失败时**保留上一份快照**，但 `StateStore` 记录 `updated_at` 与健康状态；前端可显示 `stale`。
- 每个采集器独立 try/except，日志带 `component` 字段。
- 时间：速率/功率用 `time.monotonic()` 计算 Δt；存储与展示用 `datetime.now(UTC)`。
  若检测到墙钟跳变 > 5 s，仅重置速率基线，不产生虚假尖峰。

默认周期：

| 任务 | 周期 | 说明 |
|---|---:|---|
| CPU（RAPL/temp/usage/freq） | 1 s | `collector.cpu_interval` |
| GPU | 1 s | 与 CPU 同时刻，便于对齐曲线 |
| 内存 | 2 s | |
| 磁盘 | 5 s | |
| 网络 | 1 s | |
| 系统功耗计算 | 1 s | 读取最新 CPU/GPU/meter 状态，不重新采硬件 |
| 外部电表 | 1 s 或设备最小间隔 | Phase 4 |

### 5.2 CPU / RAPL（`power/adapters/rapl.py`）

发现：

```python
def discover() -> list[RaplDomain]:
    # glob /sys/class/powercap/{control_type}:*  （默认 control_type=intel-rapl）
    # Linux 主线把 AMD Zen RAPL 也注册为 intel-rapl:*，因此无需按厂商分支
    # 读取 name -> "package-0" / "package-1"
    # 读取 max_energy_range_uj
    # 只把 name.startswith("package-") 的域计入 CPU 功耗
```

规则：

1. 子域（路径形如 `intel-rapl:0:0`，或 name 为 `dram` / `core` / `uncore`）**只读展示，不参与求和**。
   本机没有子域，但代码必须兼容。
2. 若将来出现 `dram` 子域，作为独立指标 `cpu.dram_power` 暴露，
   不加入 CPU Package 总和，也不加入整机估算（除非显式配置，避免重复计算）。
3. 每次采样都检查域集合是否变化（GPU/CPU 热插拔或驱动重载），变化时重置基线。

功率计算：

```
Δt = monotonic_now - monotonic_prev          # 秒
ΔE = cur - prev                              if cur >= prev
ΔE = cur + max_energy_range_uj - prev        if cur <  prev   # 回绕
```

- 首次采样只建立基线，`power=unavailable`（`error="warm-up"`）。
- `Δt < 0.2 s` 时跳过本轮（避免除零/噪声）。
- 合理性检查：`power = ΔE / Δt / 1e6`；若 `power > rapl_max_plausible_w`（默认 1500 W/package）
  视为计数器重置或驱动异常 → 丢弃该样本、重建基线、本轮输出 `unavailable`，并记一条 warning。
- 能量累计：`energy_j += ΔE / 1e6`，只在有效样本上累加；该值表示“采集进程启动以来”，不冒充设备终身电量。
- `enabled == 0` → 该域 `unavailable`，`error="rapl domain disabled"`。
- 读取 `EACCES` → 直接读取失败后自动切换到只读 helper；若 helper 也不可用则 `unavailable`，
  `error` 明确提示安装 helper 或使用 `CAP_DAC_READ_SEARCH`/root。

温度与拓扑：

- 温度：扫描 `/sys/class/hwmon/*/name == "coretemp"`，解析 `temp*_label == "Package id N"`；
  AMD 则读取 `k10temp` 的 `Tctl`/`Tdie`/`TccdN`，多个 hwmon 设备按拓扑中的 package id 顺序映射。
  读不到就 `unavailable`，不要用其他传感器的值顶替。
- 核心/线程数：遍历 `/sys/devices/system/cpu/cpu[0-9]+/topology/{physical_package_id,core_id}`，
  按 package 去重 `core_id`。
- 使用率：`psutil.cpu_percent(percpu=True)`（初始化时先调用一次丢弃首帧），
  按 `physical_package_id` 对逻辑 CPU 取平均。
- 频率：对 package 内逻辑 CPU 的 `scaling_cur_freq` 取平均（kHz → MHz）；
  sysfs 不可用时退回 `psutil.cpu_freq()` 并标注 source。

### 5.3 GPU（`power/adapters/nvidia.py`、`power/adapters/amdgpu.py`）

- 首选 `pynvml`（PyPI 包名 `nvidia-ml-py`）。
- 初始化：`nvmlInit()` → `nvmlDeviceGetCount()` → 逐卡取 `uuid`、`name`、PCI bus id。
- **内部以 UUID 为 key**，index 只用于枚举；GPU 顺序变化不影响历史数据关联。
- 每张卡独立 try/except：失败卡 `unavailable` + `error`，其他卡继续。
- 指标映射：

| 字段 | NVML 调用 | 单位 |
|---|---|---|
| power | `nvmlDeviceGetPowerUsage` | mW → W |
| power_limit | `nvmlDeviceGetPowerManagementLimit` / `GetEnforcedPowerLimit` | mW → W |
| temperature | `nvmlDeviceGetTemperature(..., NVML_TEMPERATURE_GPU)` | °C |
| utilization | `nvmlDeviceGetUtilizationRates().gpu` | % |
| memory | `nvmlDeviceGetMemoryInfo` | bytes |
| fan | `nvmlDeviceGetFanSpeed` | %（Tesla 常抛 NotSupported → unavailable） |
| core/mem clock | `nvmlDeviceGetClockInfo(SM/MEM)` | MHz |

- 驱动恢复：`nvmlInit` 失败时 GPU 采集器进入 `error`，按 5 s → 10 s → 30 s（封顶）退避重试，
  成功后自动恢复；不退出进程。
- GPU 数量变化：每 10 s 或在失败时重新枚举；新增卡建立基线，移除卡从快照中删除（历史保留）。
- 备用采集：`nvidia-smi --query-gpu=... --format=csv,noheader,nounits`，使用
  `asyncio.create_subprocess_exec`（绝不经 shell），按 UUID 解析；`[N/A]`、`[Not Supported]` → `None`。
  仅在 `gpu.fallback_nvidia_smi: true` 且 pynvml 初始化失败时启用。

AMD GPU（amdgpu hwmon）：

- 扫描 `/sys/class/drm/card[0-9]*/device/hwmon/hwmon*/`，通过 `vendor == 0x1002`、
  `driver`/`uevent` 或 hwmon `name` 确认 AMD 设备；不依赖 pynvml 或 `nvidia-smi`。
- 指标映射：`power1_average`（优先）或 `power1_input`（µW → W）、`power1_cap`、`temp1_input`
  （m°C → °C）、`fan1_input`（rpm，结合 `fan1_max` 换算百分比）、`gpu_busy_percent`、
  `mem_info_vram_used/total`、`pp_dpm_sclk/mclk` 当前频率。
- ID 用 PCI BDF 生成 `amdgpu:0000:03:00.0`，历史曲线 `series=gpu:<uuid>` 可继续使用。
- APU 的 hwmon 功率通常包含 CPU package，默认 `include_in_system_total=false`，只展示不参与
  整机分项求和；`gpu.amdgpu.apu_policy` 可强制 `include`/`exclude`。
- NVIDIA 与 AMD 后端独立采样；`gpu.backends` 控制启用项，`gpu.require_all_backends=false`
  时任一后端可用即视为枚举成功。最终 index 统一重排，保证 UI/数据库唯一。

### 5.4 内存 / 磁盘 / 网络

- 内存：`psutil.virtual_memory()`、`psutil.swap_memory()`。
- 磁盘：
  - 容量：`psutil.disk_partitions(all=False)` + `disk_usage(mountpoint)`，按设备去重，排除 `squashfs`。
  - 速率/IOPS：`psutil.disk_io_counters(perdisk=True)`，排除 `loop*`、`ram*`；
    对分区名映射到父设备（`nvme0n1p2` → `nvme0n1`）。
  - `read_mbps = Δread_bytes / Δt / 1e6`；IOPS = `Δ(read_count + write_count) / Δt`。
- 网络：`psutil.net_io_counters(pernic=True)`；按 `network.exclude_interfaces` 通配符过滤
  （默认 `lo`、`docker*`、`br-*`、`veth*`、`virbr*`、`tun*`、`tap*`），也可用 `include_interfaces` 覆盖。
  `rx/tx_total` 表示网卡自启动以来的累计值（来自内核计数器），不是数据库统计值。
- 所有速率共享 `RateMeter`：保存上次计数器 + monotonic 时间；计数器变小或接口消失时只重置基线，
  不输出负值。

## 6. 整机功耗计算（`power/calculator.py`）

```python
def decide(
    cpu: CpuSnapshot,
    gpus: tuple[GpuSnapshot, ...],
    meter: MeterReading | None,
    meter_state: MeterState,
    cfg: PowerConfig,
) -> SystemPower: ...
```

决策表：

| 外部电表 | CPU | GPU | 结果 |
|---|---|---|---|
| 健康 | 任意 | 任意 | `measured`，`scope=wall`，电表值优先 |
| 关闭 / 离线 | 全部实测 | 全部实测（或 0 张卡） | `estimated`，`scope=dc_components` |
| 关闭 / 离线 | 有缺失 | 任意 | 默认 `unavailable` + `missing`；`strict_estimate=false` 时才给部分和估算并标注 missing |
| 关闭 / 离线 | 全部实测 | 某张卡失败 | 默认 `unavailable`；单卡仍各自展示 |
| 电表刚离线 | — | — | 立即退回估算，`meter_status="offline"`，保留 `last_meter_at`，UI 明示 |

```text
Estimated System Power (DC components)
  = Σ CPU package power
  + Σ GPU power
  + other_components_w
```

`other_components_w` 覆盖主板、内存、风扇、磁盘、网卡等未直接测量的部件。默认不写死数值：
`power.estimated_other_power_w` 留空时，`suggest_other_power_w()` 按检测到的 socket 数、内存容量、
磁盘设备给出建议（60 W/socket + 0.12 W/GiB 内存 + 4 W/NVMe 或 6 W/其他磁盘，分区按父设备去重，封顶 400 W），
并在 `SystemPower.other_power_source` 与 `/api/v1/config` 的 `power.last_suggestion` 中公开明细。
用户在配置里写入固定值后，该值优先，`other_power_source="config"`。它**不包含 PSU 转换损耗**，
因此 `estimated(scope=dc_components)` 与电表 `measured(scope=wall)` 不应被要求数值相等；
UI 必须展示 scope 提示，避免误读。

自动建议值与固定配置值都不是测量值；UI 的 tooltip 需说明来源。APU 的 GPU hwmon 功率
已包含 CPU package，默认不计入 GPU 总和，避免重复计算。

## 7. 外部电表适配器（Phase 4，`power/adapters/`）

```python
@dataclass(frozen=True, slots=True)
class MeterCapabilities:
    adapter_id: str
    supports_power: bool
    supports_energy: bool
    supports_voltage: bool
    supports_current: bool
    supports_power_factor: bool
    min_interval_s: float

@dataclass(frozen=True, slots=True)
class MeterReading:
    timestamp: datetime
    power: Measurement[float]         # W
    energy_total: Measurement[float]  # kWh（设备累计）
    voltage: Measurement[float]       # V
    current: Measurement[float]       # A
    power_factor: Measurement[float]  # 0..1
    source: str

class PowerMeterAdapter(Protocol):
    adapter_id: str
    async def probe(self) -> MeterCapabilities: ...
    async def read(self) -> MeterReading: ...
    async def close(self) -> None: ...
```

- `shelly.py`：Gen2 `GET /rpc/Shelly.GetStatus`（`switch:0.apower`、`aenergy.total`），
  Gen1 `GET /status`（`meters[0].power`）；自动探测，只读，超时 2 s。
  Gen1 的累计电量 `total` 在不同固件中可能以 Watt-minute 表示，单位不明确，因此标记为
  `unavailable`，改用功率积分；Gen2 `aenergy.total`（Wh）转换为 kWh。
- `modbus.py`：`pymodbus`，配置 host/port/unit_id/function_code/register/scale/word_order；
  支持 uint32/float32 与高字在前/在后；兼容新版 `device_id` 与旧版 `slave` 关键字。
- `http_pdu.py`：通用 JSON-over-HTTP PDU，使用点分路径（支持数组下标）映射功率/电量/电压/电流。
- `MeterService`（`app/services/meter_service.py`）独占一个适配器：
  - 按 `external_meter.interval` 轮询，`probe()` 只执行一次；读取失败只影响电表自身；
  - 连续失败 `failover_after` 次（默认 3）→ `offline`，计算器退回估算模式；
  - 恢复后自动切回实测（`state=ok`），离线/恢复各写一次 `event_log`；
  - 保留最后成功读数、最后成功时间与失败计数，供 API/UI 展示；
  - 每秒最多写一条 `meter_sample`（网络读取频率不受写库影响）。
- 失败处理：连续失败 `failover_after` 次（默认 3）→ 计算器退回估算模式；
  恢复后自动切回，并在事件日志中记录切换。
- 电表累计电量优于功率积分：若 `energy_total` 可用且电表当前是 `scope=wall` 的来源，
  用电表差值计算 Wh 并标记 `measured`；首次读数只建立基线，计数器回退或单次跳变超过
  5000 Wh 时丢弃该差值并回退到 `power × Δt` 积分（`estimated`）。

## 8. 状态与事件（`app/state.py`）

```python
class StateStore:
    def update(self, section: str, value: object) -> None: ...
    def snapshot(self) -> Snapshot: ...
    def section_age_ms(self, section: str) -> int: ...

class EventBus:
    def publish(self, snapshot: Snapshot) -> None: ...
    def subscribe(self) -> Subscription: ...   # 有界队列
```

- `StateStore`：受 `asyncio.Lock` 保护的最新 section 字典；`snapshot()` 返回不可变快照。
- `EventBus`：每个 WS 连接一个有界队列。因为推送的是**完整快照**，慢客户端可直接丢弃旧快照
  （队列容量 1，新值覆盖旧值），天然防止内存增长和无意义积压。
- 客户端心跳：服务端每 20 s 发 `ping`，30 s 未收到 `pong` 关闭连接。

## 9. 存储设计

### 9.1 SQLite 配置

- `journal_mode=WAL`、`synchronous=NORMAL`、`busy_timeout=5000`。
- 单写者：后台 `DatabaseWriter` 任务独占写连接；读请求走独立连接或 `to_thread`。
- 写入队列 `asyncio.Queue(maxsize=300)`；满时丢最旧样本并计数告警，**绝不阻塞采集**。
- 数据库异常：记录日志、标记 `db=degraded`，继续采集与 WebSocket；恢复后自动继续写入。

### 9.2 表结构

```sql
CREATE TABLE schema_meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);

-- 1 秒原始样本（写入间隔可配置）
CREATE TABLE sample (
  ts_ms INTEGER PRIMARY KEY,
  system_w REAL, system_quality TEXT, system_source TEXT, system_scope TEXT,
  cpu_w REAL, cpu_quality TEXT,
  gpu_w REAL, gpu_quality TEXT,
  cpu_usage_pct REAL, cpu_temp_c REAL,
  gpu_usage_pct REAL, gpu_temp_c REAL,
  mem_used_b INTEGER, mem_total_b INTEGER, mem_usage_pct REAL,
  swap_used_b INTEGER,
  energy_wh REAL, energy_quality TEXT,
  meter_w REAL
);
CREATE INDEX idx_sample_ts ON sample(ts_ms);

CREATE TABLE gpu_sample (
  ts_ms INTEGER NOT NULL,
  uuid TEXT NOT NULL,
  power_w REAL, power_quality TEXT,
  temp_c REAL, util_pct REAL,
  mem_used_b INTEGER, mem_total_b INTEGER,
  power_limit_w REAL, fan_pct REAL,
  clock_mhz REAL, mem_clock_mhz REAL,
  PRIMARY KEY (ts_ms, uuid)
);

CREATE TABLE aggregate_1m (
  bucket_ms INTEGER PRIMARY KEY,
  system_w_avg REAL, system_w_max REAL,
  system_quality TEXT, system_scope TEXT,
  cpu_w_avg REAL, gpu_w_avg REAL,
  energy_wh REAL, energy_quality TEXT,
  sample_count INTEGER, expected_count INTEGER
);

CREATE TABLE aggregate_1h (
  bucket_ms INTEGER PRIMARY KEY,
  system_w_avg REAL, system_w_max REAL,
  system_quality TEXT, system_scope TEXT,
  cpu_w_avg REAL, gpu_w_avg REAL,
  energy_wh REAL, energy_quality TEXT,
  sample_count INTEGER, expected_count INTEGER
);

CREATE TABLE meter_sample (
  ts_ms INTEGER PRIMARY KEY,
  power_w REAL, energy_kwh REAL,
  voltage_v REAL, current_a REAL, power_factor REAL,
  source TEXT
);

CREATE TABLE event_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts_ms INTEGER NOT NULL,
  level TEXT NOT NULL,
  component TEXT NOT NULL,
  message TEXT NOT NULL
);
```

保存策略：

- Schema 版本 `SCHEMA_VERSION=3`。Phase 4 为 `aggregate_1m/1h` 增加 `system_quality`/`system_scope`；
  启动时通过 `PRAGMA table_info` 做增量 `ALTER TABLE ADD COLUMN`，不删除、不改写已有数据。
- 原始样本默认每 1 s 一行；`database.write_interval` 可调大以降低写入量。
- 不可用值写 `NULL` + `quality='unavailable'`，不写 0。
- `energy_wh`：该样本覆盖区间的电量；功率缺失或间隔异常时为 `NULL`。
- `meter_sample` 仅在电表读取成功时写入（默认最多 1 条/秒），保留期与原始样本相同（默认 7 天）。
- 磁盘/网络历史不在 MVP 持久化（PRD 只要求功耗/能耗历史），只保留当前值。

### 9.3 聚合与保留

- 每分钟第 5 秒计算上一分钟：`aggregate_1m`（avg/max/min、`energy_wh` 求和、
  `sample_count`、`expected_count`）。用 `INSERT ... ON CONFLICT DO UPDATE` 保证幂等。
- 每小时计算上一小时：从 `aggregate_1m` 汇总到 `aggregate_1h`。
- 服务停机导致的空桶不补零；API 返回缺口，前端显示断点。
- 每天 `cleanup_hour`（默认 03:00）执行分批删除：
  raw > 7 天、1m > 90 天、1h > 365 天；每批 10 000 行、批间让出事件循环。

### 9.4 查询

- 5 m / 15 m / 1 h：读 `sample`（必要时服务端按 10 s 桶降采样）。
- 6 h / 24 h：读 `aggregate_1m`（24 h 最多 1440 点）。
- 7 d / 30 d / 365 d：读 `aggregate_1h`。
- 所有响应限制 ≤ 2000 点；超过则继续合并桶。

## 10. API

根路径端点：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/`、`/cpu`、`/gpu`、`/energy` | Web UI 页面 |
| GET | `/api` | 服务名、版本、API/WebSocket 入口 |
| GET | `/health` | 存活与依赖状态（供 systemd/监控） |
| GET | `/docs` | OpenAPI 交互式文档 |
| GET | `/static/*` | CSS、ES modules、本地 ECharts |

### 10.1 REST（`/api/v1`）

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/system` | 主机信息、运行时长、采集器健康、数据库状态 |
| GET | `/cpu` | CPU 快照（含每 package） |
| GET | `/gpu` | GPU 列表（按 UUID） |
| GET | `/power/current` | 整机/CPU/GPU 功耗 + quality/source/scope + `meter` 状态 |
| GET | `/meter` | 外部电表状态、能力、最后读数、最后成功时间、失败计数 |
| GET | `/power/history?window=5m&series=system,cpu,gpu_total,gpu:<uuid>` | 曲线数据，返回 `[[ts_ms, value], ...]` |
| GET | `/energy/daily?days=7` | 每日 kWh（本地时区边界） |
| GET | `/energy/weekly` | 最近 7 天汇总 |
| GET | `/energy/monthly` | 最近 30 天汇总 |
| GET | `/energy/summary` | 今日/昨日/7d/30d + 电费 |
| GET | `/config` | 安全子集：电价、货币、估算参数、采集周期、电表间隔/切换阈值 |
| GET | `/diagnostics` | RAPL/NVML/DB 探测结果与错误原因（Phase 1 CLI 复用） |

Web UI 位于根路径 `/`、`/cpu`、`/gpu`、`/energy`；服务元数据位于 `/api`，`/health` 也在根路径。
其余 JSON 端点位于 `/api/v1`。所有路由均为普通 `def`（FastAPI 线程池执行），
数据库读写不会阻塞 WebSocket 事件循环。

功耗字段统一结构：

```json
{
  "value": 512.4,
  "unit": "W",
  "quality": "estimated",
  "source": "calculator:components",
  "timestamp": "2026-11-15T16:00:00+08:00",
  "scope": "dc_components",
  "missing": []
}
```

历史响应：

```json
{
  "window": "1h",
  "resolution": "10s",
  "series": [
    {"id": "system", "unit": "W", "quality": "estimated", "points": [[1763193600000, 512.4], [1763193610000, 518.1]]},
    {"id": "gpu:GPU-xxxx", "unit": "W", "quality": "measured", "points": []}
  ]
}
```

`points` 中缺测用 `null`，不插值。

### 10.2 WebSocket `/ws/realtime`

连接后先发：

```json
{"type": "hello", "protocol": 1, "interval_ms": 1000}
```

随后每秒推送完整快照（结构同 REST，附加 `type: "snapshot"`）：

```json
{
  "type": "snapshot",
  "timestamp": "2026-11-15T16:00:00+08:00",
  "system": {
    "power": {"value": 520.5, "unit": "W", "quality": "estimated", "source": "calculator:components", "scope": "dc_components", "timestamp": "..."},
    "other_components_w": 120.4,
    "other_power_source": "auto:1 socket(s), 31.4 GiB RAM, 1 detected device(s), 1 GPU(s) measured separately"
  },
  "cpu": {"total_power": {"value": 185.2, "unit": "W", "quality": "measured", "source": "rapl:package-*", "timestamp": "..."}, "usage_percent": 35.5, "packages": []},
  "gpu": [
    {"uuid": "GPU-xxxx", "name": "Tesla V100", "include_in_system_total": true, "power": {"value": 120.5, "unit": "W", "quality": "measured", "source": "nvml:GPU-xxxx", "timestamp": "..."}, "temperature": 55, "utilization": 65}
  ],
  "health": {"gpu": {"state": "ok"}}
}
```

语义：**完整快照**，不是增量。客户端断线重连后无需补偿。

## 11. 前端

- 无构建步骤：原生 ES modules + ECharts（本地 `app/static/vendor/`，不依赖 CDN，适合内网）。
- 深色科技风；CSS Grid + 媒体查询，桌面三列、平板两列、手机单列。
- 页面：Dashboard、CPU、GPU、能耗统计；共享 `base.html` 导航与连接状态。
- 功耗数值必须显示质量徽标：`Measured`（绿）、`Estimated`（琥珀）、`Unavailable`（灰），
  并在 tooltip 中展示 `source`、`timestamp`、`scope`。
- 实时曲线：
  - 默认 5 分钟；可选 5 m / 15 m / 1 h / 6 h / 24 h。
  - 图例点击开关；`tooltip.trigger="axis"`；CPU、GPU0、GPU1、GPU Total、System 分色。
  - 5 m 视图用 WS 追加；更长窗口先 REST 拉历史，之后每 60 s 重新拉取（避免把 1 s 点无限堆进浏览器）。
  - 前端环形缓冲上限 2000 点，超出丢弃最旧点；切换时间范围时先 `chart.clear()`。
- WebSocket 客户端：指数退避重连（1/2/4/8/15 s + 抖动），页面重新可见时立即重试；
  顶部显示 `Live / Reconnecting / Offline`；重连成功后重新拉取当前窗口历史。
- 不允许 `eval`、`innerHTML` 注入后端字符串；全部用 `textContent`。
- 能耗页：今日/昨日/7 天/30 天、每小时平均功耗、趋势图；
  `Cost = kWh × price_per_kwh`，用 `Intl.NumberFormat` 按货币格式化。

## 12. 配置

`config/config.yaml`（示例见 `config/config.example.yaml`）。Phase 1 仅支持 YAML：

```
默认值 < config.yaml
```

用 pydantic 严格校验（未知字段直接报错）；配置错误启动即失败并打印明确字段。
环境变量 `PM_*` 覆盖在 Phase 2 加入。运行时只读（MVP）；电价修改暂不提供写接口。

关键新增项（相对 PRD 示例）：

- `server.port` 默认 `8010`（按本机决定）。
- `power.strict_estimate`：缺失组件时是否仍给估算值，默认 `true` 表示**不给**。
- `power.estimated_other_power_w`：留空（默认）时自动建议，写入数值则固定；来源见 `other_power_source`。
- `gpu.backends`：默认 `["nvidia", "amdgpu"]`；`gpu.require_all_backends=false` 时任一后端健康即可。
- `gpu.amdgpu.apu_policy` / `apu_vram_limit_gb`：APU 去重策略。
- `rapl.control_types`：默认 `["intel-rapl"]`，AMD Zen RAPL 也在此控制类型下。
- `rapl.max_plausible_w`：RAPL 重置检测阈值。
- `rapl.helper` / `rapl.use_sudo`：只读 RAPL helper（auto/always/never），不保存密码。
- `network.exclude_interfaces` / `include_interfaces`。
- `collector.timeout`：单次采集超时。
- `database.write_interval`、`busy_timeout_ms`。
- `history.cleanup_hour`。
- `server.timezone`：能耗按本地日期切分（默认系统时区）。
- `server.log_file`：留空写 stderr/journald；设置路径则使用轮转文件。
- `server.log_max_bytes` / `log_backup_count`：文件日志大小与保留份数。
- `power.external_meter`：`enabled/type/timeout/interval/failover_after`；`shelly`（url、generation）、
  `modbus`（host/port/unit_id/function_code/register/register_type/word_order/scale）、
  `http_pdu`（url、点分路径与缩放）。

## 13. 失败与降级语义

| 场景 | 行为 |
|---|---|
| 无 RAPL | CPU 功耗 `unavailable`，服务正常；CLI/诊断给出原因 |
| `energy_uj` 权限不足 | 自动切换到只读 helper；仍失败则 `unavailable`，错误信息给出安装指引 |
| 无 GPU 后端检测到设备 | GPU 列表为空，GPU 总功耗 0 W `measured` |
| NVML 初始化失败 | NVIDIA 后端不可用并退避重试；若 AMD 后端正常，整机估算不受影响 |
| 无 AMD hwmon 节点 | AMD 后端视为“未安装 GPU”，不标记为枚举失败 |
| 单张 GPU 读取失败 | 该卡 `unavailable`，其他卡正常；该卡不计入 GPU 总和 |
| APU 被检测到 | 仍展示功率/温度等指标，`include_in_system_total=false`，避免与 RAPL 重复计算 |
| GPU 热插拔 / 顺序变化 | 以 UUID 为准；新增卡建基线，旧卡移除 |
| 温度传感器缺失 | 温度 `unavailable`，不用其他值替代 |
| 风扇不支持（Tesla） | `fan_speed=unavailable`，正常现象 |
| 第一次 RAPL 采样 | `power=unavailable`（warm-up），1 秒后出值 |
| RAPL 回绕 | 用 `max_energy_range_uj` 修正，不产生负值/尖峰 |
| 计数器重置/异常跳变 | 丢弃该样本，重建基线，记 warning |
| 电表离线 | 连续 N 次失败后退回估算，UI 标注 `meter offline` 与最后成功时间 |
| 电表恢复 | 自动切回 `measured`，事件日志记录 |
| SQLite 锁/损坏/不可写 | 写入队列丢弃并告警；采集与 WS 继续；恢复后继续写 |
| 服务重启 | 历史保留；实时能量累计从 0 开始，UI 标注“本次运行” |
| WS 客户端慢 | 有界队列丢弃旧快照（完整快照，无副作用） |
| 时钟跳变 | 速率用 monotonic；检测到跳变重置基线 |
| 采集器卡死 | `to_thread` + 超时 + `_inflight` 防堆积，标记 degraded |

## 14. 测试策略

`pytest` + `pytest-asyncio`。硬件全部可替换：

- **RAPL**（临时目录模拟 sysfs）：
  - 多 package 分别计算；
  - 回绕（`cur < prev`）正确；
  - 首次采样 warm-up；
  - 计数器异常跳变被拒绝；
  - 子域 `dram` 不参与求和；
  - 权限错误 → `unavailable`；
  - 无 RAPL 目录 → `unavailable`。
- **GPU**（假 NVML 模块）：
  - 0 张卡；
  - 2 张卡按 UUID 输出；
  - 单卡抛异常不影响另一张；
  - 风扇 NotSupported → `unavailable`；
  - NVML 初始化失败 → 退避重试，服务不崩；
  - 卡顺序变化后 UUID 仍正确。
- **AMD GPU / 温度**（临时 sysfs）：
  - `power1_average` / `power1_input` µW 转换；
  - APU 排除与配置覆盖；
  - 非 AMD 设备跳过、缺失指标 `unavailable`；
  - NVIDIA + AMD 混合枚举与 index 重排；
  - `k10temp` 多 hwmon 到 package id 的映射。
- **计算器**：
  - 电表健康 → `measured/wall`；
  - 电表离线 → `estimated/dc_components` + `meter offline`；
  - 缺失组件 → `unavailable`（默认）；
  - 0 GPU 时 GPU 总功耗 = 0 W measured；
  - 估算值永远不会变成 `measured`。
- **电表适配器**（`httpx.MockTransport` / 假 Modbus 客户端）：
  - Shelly Gen2 功率/电压/电流/累计电量解析；
  - Shelly Gen1 功率解析且累计电量标记 `unavailable`；
  - 自动探测 Gen2 失败后回退 Gen1；
  - Modbus uint32/float32、大小字序、缩放、NaN、错误响应、旧版 `slave` 兼容；
  - HTTP PDU 点分路径/数组下标/缩放与缺失字段；
  - `MeterService` 成功、连续失败切换、恢复事件、最后读数保留与写库节流；
  - 电表累计电量差值优先，计数器重置/异常跳变回退积分。
- **服务/API**：
  - 无任何硬件时应用正常启动，REST 200，字段为 `unavailable`；
  - WS 断开重连；
  - 慢客户端不导致内存增长；
  - DB 重启后历史仍在；
  - DB 不可写时采集继续；
  - `/history` 点数上限与缺口处理。
- **日志/部署**：
  - `RotatingFileHandler` 写入、大小上限与备份数量；
  - `./scripts/run.sh check` 只读校验配置；
  - `scripts/backup.py` 在线备份 + `integrity_check`；
  - `scripts/stability_check.py` 检查存活、HTTP、RSS 与数据库增长。
- **长时间稳定性**（Phase 5，`scripts/stability_check.py`）：
  - RSS 增长在阈值内；
  - 前端 5 分钟环形缓冲大小恒定（Playwright 检查）；
  - 数据库体积符合保留策略。
- **人工验收**：
  - 与 `nvidia-smi` 对照 GPU 功率；
  - 与外部电表对照（Phase 4），并说明 DC 估算与 wall 实测的差异来源。

对应 PRD §11 的 10 条测试，全部映射到上述用例。

## 15. 分阶段实施计划

### Phase 1：硬件采集核心（已完成）

状态：代码、CLI、单元测试与真机验证均已完成；RAPL/CPU/内存/磁盘/网络/GPU 已在本机验证。

交付：

1. `pyproject.toml` / `requirements.txt` / 项目内 `.venv`。
2. `app/domain.py`、`app/config.py`（含 `/diagnostics` 所需探测函数）。
3. `power/adapters/rapl.py`、`power/adapters/nvidia.py`。
4. `collectors/{base,cpu,gpu,memory,disk,network,system}.py`。
5. `power/calculator.py`。
6. `app/cli.py`：
   - `pm doctor`：打印 RAPL 域/权限、NVML 版本/GPU、hwmon、依赖状态；
   - `pm sample --count 3 --interval 1`：打印每包/每卡功率与整机估算，含 quality。
7. 单元测试（RAPL/NVML/计算器/速率）。

实际实现补充：

- RAPL 权限通过 `deploy/rapl-read` + `scripts/install-rapl-helper.sh` 解决，不保存密码；
- CLI 入口为 `python -m app.cli`，`scripts/run.sh` 负责激活 `.venv`；
- 支持 `gpu_available` 语义：区分“没有 GPU”和“GPU 枚举失败”，后者不得按 0 W 估算；
- 测试：`38 passed`。

出口标准：

- `pm doctor` 在本机报告所有采集器与依赖的真实状态；
- 经只读 helper 读取时 2 个 package 均输出非零、合理的功率；
- 2 张 V100 的功率/温度/显存与 `nvidia-smi` 一致；
- 无权限 / 无 GPU 时输出 `unavailable` 且进程不崩；
- 回绕、单卡失败、零 GPU 测试通过。

> 说明：`energy_uj` 仍不能直接以 `jimmy` 读取，已通过只读 helper（作用域内 `NOPASSWD` sudo）解决；
> 服务与 CLI 均以普通用户运行即可测得 RAPL 功率。

### Phase 2：Web 后端（已完成）

状态：代码、测试与本机真实服务验证已完成。

交付：

- SQLAlchemy 2.0 模型 + repository + 有界写队列 + WAL。
- `CollectorService` 编排与 `StateStore` / `EventBus`。
- REST + WebSocket + 聚合/保留任务。
- `AppDatabase` 懒打开、失败重试、`degraded` 状态；磁盘故障不终止采集。
- `HistoryService` 窗口选择/降采样、`EnergyService` 时区日边界与费用、`StatisticsService` 1m/1h 聚合、
  `RetentionService` 分批清理。
- API 层只依赖 `Runtime`，不直接导入采集器。

实际实现补充：

- 存储使用同步 SQLAlchemy + `asyncio.to_thread`，单写者任务独占写连接；读请求使用独立连接。
- 写队列 `database.queue_size` 默认 300；满时丢最旧样本并计数，绝不阻塞采集。
- WebSocket 每个连接一个容量为 1 的快照队列；慢客户端跳过中间帧。
- 原始样本与聚合的分界按 raw retention cutoff 划分，`energy_between` 不会重复计数。
- 历史窗口：`5m/15m/1h` raw；`6h/24h` 1m（无聚合回退 raw）；`7d/30d/365d` 1h。
- 修正小时聚合 SQL 必须按小时表达式分组（不能按 `bucket_ms` 源列分组，否则每分钟各成一组）。
- 启动时按原始/分钟保留窗口回填聚合，长时间停机后重启不会留下聚合空洞。
- 写库节流加入少量调度容差，避免 1 s 采集循环的亚毫秒抖动把写入频率减半。
- 测试：Phase 1 + Phase 2 共 `115 passed`（Phase 3 完成后 `129 passed`；Phase 5 完成后全量 `163 passed`）。

出口验证（本机）：

- 真实服务连续 10 s 写入 10 条原始样本（1 Hz）；
- 5 分钟历史按秒返回 system/cpu/gpu 曲线；
- 重启后历史仍可查询且最旧样本保留；
- WebSocket 连续两次连接均收到 `hello` + `snapshot`；
- 数据库路径不可打开时 `/health` 为 `degraded`、历史接口 `503`，WebSocket 仍推送；
- GPU 驱动故障时系统功率/能量为 `unavailable`，`missing_components=["gpu"]`，进程不崩溃。


### Phase 3：Web 前端（已完成）

状态：代码、自动化测试与本机 Playwright 桌面/手机验证已完成。

交付：

- 四个页面：`/` 总览、`/cpu`、`/gpu`、`/energy`；共享 `base.html` 导航、连接状态与页脚。
- 无构建步骤：Jinja2 服务端模板 + 原生 ES modules；ECharts 5.5.1 本地 vendored
  （`app/static/vendor/echarts.min.js`，附 Apache-2.0 许可证），不依赖 CDN。
- 质量徽标：`Measured` 绿、`Estimated` 琥珀、`Unavailable` 灰；tooltip 展示
  `source / time / scope`，由固定定位的共享 tooltip 元素渲染，不写 `innerHTML`。
- 实时曲线：默认 5 分钟；可选 5 m / 15 m / 1 h / 6 h / 24 h。
  - 5 m 视图把 WS 快照追加到每序列 2000 点环形缓冲。
  - 更长窗口从 REST 拉取并每 60 s 刷新；切换范围先 `chart.clear()`。
  - 页面重新可见或 WebSocket 重连后重新拉取当前窗口。
- WebSocket 客户端：指数退避 1/2/4/8/15 s + 抖动；浏览器 `online` 事件立即重连；
  顶部状态 `Live / Reconnecting / Offline`。
- 能耗页：今日/昨日/7 天/30 天卡片、最近 30 天每日 kWh 柱状图、最近 24 小时每小时平均功率柱状图；
  费用由 `kWh × price_per_kwh` 计算，未启用电价时显示 `—`。
- 响应式：桌面三列/两列、手机单列；无横向溢出；缺失值显示 `—` 而不是 0。

实际实现补充：

- 根路径改为 HTML 仪表盘；原 JSON 服务索引迁移到 `GET /api`。
- 页面模板在 `app/templates/`，前端资源在 `app/static/`（通过 `/static` 挂载）。
- 共享前端模块 `app/static/assets/core.js` 统一封装 DOM 写入、质量徽标、API、历史视图与 WebSocket。
- 历史序列质量元数据按底层样本聚合：全部 `measured` 时返回 `measured / wall`，
  混合或估算样本返回 `estimated / dc_components`；Phase 4 已完成该切换。
- 测试：当时全量 `129 passed`；Phase 5 完成后全量 `163 passed`（新增 `tests/test_web.py` 覆盖页面、静态资源、无 CDN 与许可证）。

出口验证（本机）：

- 四个页面均 200，共享导航正确高亮；Playwright 无 console/page error。
- 1440 px 与 390 px 均无横向溢出；`#conn-text` 进入 `Live`。
- 15 m / 24 h 范围切换后重新拉取历史并更新元数据。
- 离线事件显示 `Offline`，恢复在线后回到 `Live`。
- GPU 不可用时系统功率/能耗徽标为 `Unavailable`，数值 `—`。

### Phase 4：外部电表（已完成）

状态：代码、单元测试、模拟电表端到端与 Playwright 桌面/手机验证已完成；真实电表待接入。

交付：

- `PowerMeterAdapter` 契约与工厂（`power/adapters/base.py`）。
- Shelly Gen1/Gen2、Modbus TCP、通用 HTTP PDU 适配器。
- `MeterService`：轮询、超时、连续失败切换、自动恢复、状态发布与事件日志。
- 电表实测优先：`quality=measured`、`scope=wall`；累计电量优先于功率积分。
- API：`GET /api/v1/meter`、`/power/current` 的 `meter` 字段、`/health` 的 `meter` 状态；
  WebSocket 快照携带 `meter`。
- 数据库：`meter_sample` 表、`sample.meter_w`、聚合质量/scope 列（schema v3 增量迁移）。
- UI：总览电表状态/功率/累计电能/来源/最后成功/失败计数与离线告警；
  能耗页实时条显示 `墙插实测`、电能来源与回退状态。

实际实现补充：

- 连续失败达到 `failover_after` 才标记 `offline`；之前为 `retrying`（有最后读数）或
  `starting`（从未成功），避免瞬时抖动立即切走。
- 离线/恢复事件只在状态跨越阈值时各写一次，避免刷屏。
- 电表读数自带时间戳，计算器与 API 使用读数时间而非快照时间，避免陈旧值伪装成新数据。
- 历史/聚合查询保留 `system_quality`/`system_scope`，仅当桶内全部为实测墙插功率时返回
  `measured / wall`。

出口验证（本机）：

- 模拟 Shelly Gen2：系统功率 `420 W / measured / wall`，累计电量差值约 `1 Wh/s`，
  `meter_sample`、`sample.meter_w`、`event_log` 均正确写入。
- 停止模拟电表：3 次失败后 `state=offline`、系统退回估算、UI 告警可见且保留最后读数。
- 恢复模拟电表：自动回到 `ok`，写入 recovery 事件。
- 默认配置（电表关闭）：`state=disabled`，不影响 CPU/GPU 采集、历史与 UI。
- 测试：全量 `163 passed`，新增 `tests/test_adapters.py`、`tests/test_meter_service.py`
  以及电表 API、聚合质量与历史 scope 用例。

### Phase 5：部署与优化（已完成）

状态：systemd unit、安装脚本、日志轮转、备份脚本、稳定性检查均已完成并在本机验证。

交付：

- `deploy/power-monitor.service`：systemd unit 模板（运行用户、配置路径、自动重启、journald 日志）。
- `scripts/install-service.sh`：按当前 checkout 与用户生成 unit，启用并启动服务；
  不写入密码或敏感信息。
- 日志：默认 stderr → journald；`server.log_file` 可切换到 `RotatingFileHandler`，
  由 `server.log_max_bytes` / `server.log_backup_count` 控制。
- `scripts/backup.py` / `scripts/backup.sh`：使用 sqlite3 backup API 在线备份，
  `PRAGMA integrity_check` 校验并打印行数，默认保留 14 份。
- `scripts/stability_check.py`：监控存活、HTTP、RSS 与数据库增长，输出 PASS/FAIL 退出码。
- `./scripts/run.sh check`：只读校验配置并打印生效设置（不访问硬件）。

本机验证：

- unit 安装后 `active (running)`，开机自启；服务以 `jimmy` 运行；
  journal 显示 `sudo ... rapl-read`，CPU 功率 `measured`。
- `SIGKILL` 主进程后 systemd 自动拉起新 PID，`/health` 恢复 200。
- 30 s 稳定性检查：RSS +0.8 MiB、数据库体积稳定、HTTP 失败 0、`RESULT: PASS`。
- 在线备份 37 MiB 数据库，`integrity_check=ok`，`sample/aggregate_1m/aggregate_1h` 行数正常。
- 测试：全量 `163 passed`，新增 `tests/test_logging.py` 覆盖文件日志与 `check` 命令。

## 16. 部署与运维（Phase 5）

### 16.1 systemd

`deploy/power-monitor.service` 是模板，`scripts/install-service.sh` 使用当前 checkout 路径和
`SUDO_USER`（或 `PM_USER`）生成 `/etc/systemd/system/power-monitor.service`：

- 以普通用户运行，不使用 root；`WorkingDirectory` 与 `PM_CONFIG` 指向本仓库与 `config/config.yaml`。
- `Restart=on-failure`、`RestartSec=5s`、`TimeoutStopSec=20s`，`KillSignal=SIGINT` 触发优雅退出。
- 标准输出/错误写入 journald；`ReadWritePaths` 仅允许写 `data/` 与 `logs/`。
- 不设置 `NoNewPrivileges`，否则作用域内 `sudo` 无法执行只读 RAPL helper。
- 安装脚本只写 unit 文件与目录权限，不保存密码或凭据。

### 16.2 日志

- 默认：stderr → journald，由 journald 的 `SystemMaxUse` 等策略轮转。
- 可选：`server.log_file` + `server.log_max_bytes` / `server.log_backup_count` 使用
  `RotatingFileHandler`；相对路径基于项目根目录。使用文件日志时需把目录加入 unit 的
  `ReadWritePaths`。
- 日志格式：时间、级别、logger 名称、消息；`server.log_level=debug` 时开启 HTTP access log。

### 16.3 数据清理与备份

- `RetentionService` 每天在 `history.cleanup_hour` 清理：原始样本/GPU/电表样本
  `raw_retention_days`，1 分钟聚合 `minute_retention_days`，1 小时聚合
  `hourly_retention_days`；每批 10,000 行，避免长事务阻塞写入。
- `scripts/backup.py` 使用 sqlite3 在线 backup API（WAL 安全），完成后执行
  `PRAGMA integrity_check` 并打印行数；默认写入 `backups/`，保留最近 14 份。
- 恢复：停止服务 → 用备份覆盖 `data/power_monitor.db` → 启动服务。
- 备份目录与文件日志均不进入版本库（`.gitignore`）。

### 16.4 稳定性

`scripts/stability_check.py` 通过监听端口自动定位进程，定时采样：

- 进程存活与 `/health`、`/api/v1/power/current` HTTP 可用性；
- RSS（MiB）与数据库文件（含 `-wal`/`-shm`）体积；
- 默认阈值：RSS 增长 ≤64 MiB、数据库增长 ≤512 MiB（可参数覆盖）。

输出逐次采样与 `RESULT: PASS/FAIL`，失败时返回非零退出码。前端 2000 点环形缓冲与
5 分钟窗口由 Playwright 检查覆盖。

## 17. 风险与开放问题

| 风险 | 说明 | 缓解 |
|---|---|---|
| RAPL 权限 | 本机 `energy_uj` 仅 root | systemd capability 或 root；采集器明确降级 |
| RAPL 语义 | 无 DRAM 域，package 是否含 DRAM 不确定 | 不作声明；只报 package |
| 估算偏差 | DC 组件和 ≠ 墙插功率 | UI 展示 `scope`；电表实测优先 |
| other_components 默认值 | 固定常数对未知硬件可能明显偏差 | 默认按硬件自动建议并公开明细；可写入固定值覆盖 |
| NVML 版本 | 曾出现内核模块/用户态 mismatch；重启后加载 580.178.04，2 张 V100 已真机验证 | 驱动升级后跑 `pm doctor` 与 `nvidia-smi` 复核 |
| GPU 风扇 | Tesla 无风扇转速 | 标记 unavailable，不显示 0 |
| 无认证 | MVP 局域网内无鉴权 | 绑定内网/防火墙；后续加 token |
| SQLite 写入量 | 1 s 一行，7 天约 60 万行 | WAL + 保留策略；实测体积 |
| 时间跳变 | 影响功率积分 | monotonic 计算 Δt，检测跳变 |
| 前端内存 | 实时点无限累积 | 环形缓冲 2000 点 + 长窗口走历史 API |

已确认（见 `docs/ENVIRONMENT.md` §7）：D1 运行身份、D2 GPU 驱动修复（已重启并真机验证）、
D3 其他部件功耗默认值、D4 项目 `.venv`、D5 监听地址/防火墙。
