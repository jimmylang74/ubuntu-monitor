# 环境检查报告

- 检查时间：2026-11-15
- 复核时间：2026-11-18（主机重启后，GPU 阻塞项已解除，见 §9）
- 主机目录：`/home/jimmy/code/power-monitor`
- 检查方式：只读探测（未修改系统、未安装软件、未重启服务）

## 1. 结论摘要

| 项目 | 探测结果 | 对项目的影响 |
|---|---|---|
| 操作系统 | Ubuntu 24.04.4 LTS (noble) | ✅ 符合目标平台 |
| 内核 | 初检 `6.8.0-136-generic`；复核时已运行 `6.8.0-149-generic` | ✅ |
| 虚拟化 | `systemd-detect-virt` = `none`，物理机 | ✅ 可读取真实 RAPL |
| CPU | 2 × Intel Xeon E5-2696 v4，22 核 / 44 线程每颗，共 88 线程 | ✅ 需要多 Package 支持 |
| RAPL | 存在 2 个 package 域，`enabled=1` | ⚠️ 计数文件仅 root 可读（已用只读 helper 解决） |
| NVIDIA GPU | 2 × Tesla V100 SXM2 16GB（PCI `02:00.0`、`03:00.0`） | ✅ 重启后 NVML 可用（见 §9） |
| 温度 | 2 × `coretemp`（Package id 0/1）+ NVMe | ✅ 可读 |
| 内存 | 125.76 GiB，Swap 8 GiB | ✅ |
| 磁盘 | `nvme0n1` 1.9T（根分区）、`sda` 931.5G、`sdb` 4.5T | ✅ |
| 网络 | `lan0`、`lan1` 启用；另有 Docker bridge / veth | ✅ 默认过滤虚拟网卡 |
| Python | 3.12.3（venv：`/home/jimmy/venv`） | ✅ |
| 权限 | 当前用户 `jimmy`（uid 1001，属 sudo 组），sudo 需要密码 | ⚠️ 无法直接验证 RAPL |

## 2. CPU 与 RAPL

### 2.1 CPU

```
Architecture: x86_64
Model name:   Intel(R) Xeon(R) CPU E5-2696 v4 @ 2.20GHz
CPU(s):       88
Socket(s):    2
Core(s)/socket: 22
Thread(s)/core: 2
NUMA nodes:   2
NUMA node0:   0-21,44-65
NUMA node1:   22-43,66-87
cpufreq driver: intel_cpufreq
scaling_cur_freq (cpu0): 3.15 GHz
```

CPU 到插槽的映射应通过
`/sys/devices/system/cpu/cpu<N>/topology/physical_package_id` 获取，
不能假设 `cpu < 44` 属于 package 0（本机 NUMA 线程分布已跨越 44）。

### 2.2 RAPL / powercap

```
/sys/class/powercap/intel-rapl:0  name=package-0  max_energy_range_uj=262143328850
/sys/class/powercap/intel-rapl:1  name=package-1  max_energy_range_uj=262143328850
/sys/class/powercap/intel-rapl/enabled = 1
```

- 只有两个顶层 `package-*` 域，**没有** `dram`、`core`、`uncore` 子域。
- 因此无法单独测量内存功耗；系统估算中也不会虚构 DRAM 功耗。
- Broadwell-EP 的 package 域是否包含 DRAM 功耗没有可靠结论，文档中不做此声明。
- `max_energy_range_uj = 262143328850` µJ ≈ 262 kJ ≈ **72.8 Wh**。
  以 300 W 计约 14.5 分钟回绕一次，以 600 W 计约 7.3 分钟回绕一次，**必须实现回绕处理**。

权限：

```
-r-------- 1 root root 4096 ... /sys/class/powercap/intel-rapl:0/energy_uj
-r-------- 1 root root 4096 ... /sys/class/powercap/intel-rapl:1/energy_uj
```

以 `jimmy` 读取得到 `Permission denied`。`name`、`max_energy_range_uj`、`enabled` 可读。
`chmod` 对 sysfs 属性无效，解决方案见 §6。

## 3. NVIDIA GPU

### 3.1 硬件

```
02:00.0 3D controller: NVIDIA GV100GL [Tesla V100 SXM2 16GB] [10de:1db1] (rev a1)
03:00.0 3D controller: NVIDIA GV100GL [Tesla V100 SXM2 16GB] [10de:1db1] (rev a1)
/dev/nvidia0, /dev/nvidia1, /dev/nvidiactl, /dev/nvidia-uvm 均存在，权限 crw-rw-rw-
```

### 3.2 NVML 驱动/库版本不一致（初检阻塞项，已解决）

> 2026-11-18 复核：主机已重启，加载新内核模块 `580.178.04`，NVML 与 `nvidia-smi` 均可用。
> 以下为 2026-11-15 初检记录，保留作为故障对照；最新实测见 §9。

```
$ nvidia-smi
Failed to initialize NVML: Driver/library version mismatch
NVML library version: 580.178
```

诊断：

| 组件 | 版本 |
|---|---|
| 已加载内核模块 `/sys/module/nvidia/version` | `580.173.02` |
| 用户态 NVML / nvidia-smi | `580.178` |
| DKMS 已安装模块 | `580.178.04`（for `6.8.0-136-generic` 与 `6.8.0-149-generic`） |
| 运行内核 | `6.8.0-136-generic` |

即：驱动包已升级，但内存中仍是旧模块。DKMS 已为当前运行内核构建好新模块，
**重启即可恢复**。模块引用计数较高（`nvidia 93`、`nvidia_uvm 2`），
且本机有 `ollama` 等容器可能在用 GPU，因此不建议在业务时段直接卸载模块。

恢复后需用以下命令确认所有指标可读：

```bash
nvidia-smi --query-gpu=uuid,name,power.draw,power.limit,temperature.gpu,utilization.gpu,\
memory.used,memory.total,fan.speed,clocks.sm,clocks.mem --format=csv
```

预期 `fan.speed` 对 Tesla V100 返回 `[N/A]`，这是正常现象，采集层应标记为 `unavailable`。

## 4. 温度、内存、磁盘、网络

### 4.1 温度（hwmon 直接读取，无需 lm-sensors）

```
hwmon1 coretemp: temp1 "Package id 0" = 29.0 °C
hwmon2 coretemp: temp1 "Package id 1" = 31.0 °C
hwmon0 nvme:     temp1 "Composite"    = 40.85 °C
```

`lm-sensors`（`sensors` 命令）未安装，但本项目直接读取 `/sys/class/hwmon`，不受影响。

### 4.2 内存

```
Mem:  125.76 GiB total, 20 GiB used, 105 GiB available
Swap: 8.0 GiB total, 544 MiB used
```

### 4.3 磁盘

```
nvme0n1  1.9T  KXG50PNV2T04 TOSHIBA   （/ 挂载于此，已用 540G / 31%）
sda      931.5G Colorful SL500 1
sdb      4.5T  ST5000LM000-2AN1
```

`psutil.disk_io_counters(perdisk=True)` 可见 `nvme0n1` 等设备，可计算读写速率与 IOPS。
另有 `loop0..7`，采集时应排除。

### 4.4 网络

启用：`lan0`、`lan1`。
存在且应默认隐藏：`docker0`、`br-*`、`veth*`。
配置项 `network.exclude_interfaces` 默认过滤这些接口（PRD §2.5）。

## 5. 软件环境

venv：`/home/jimmy/venv`（Python 3.12.3）

| 包 | 共享 venv | 项目 `.venv` |
|---|---|---|
| psutil | ✅ 7.2.2 | ✅ 7.2.2 |
| fastapi / uvicorn | ✅ | ✅ FastAPI 0.163.0 / Uvicorn 0.54.0 |
| pydantic / pydantic-settings | ✅ | ✅ |
| jinja2、httpx、aiohttp、PyYAML | ✅ | PyYAML ✅，httpx 0.28.1 ✅；jinja2/aiohttp 尚未使用 |
| `nvidia-ml-py`（pynvml） | ❌ | ✅ 13.615.71 |
| SQLAlchemy | ❌ | ✅ 2.1.8 |
| pytest / pytest-asyncio | ❌ | ✅ |
| pymodbus（Phase 4） | ❌ | Phase 4 安装 |

项目内 `.venv` 已创建，避免污染共享的 `/home/jimmy/venv`。

## 6. 阻塞项与处理建议

### B1. RAPL `energy_uj` 仅 root 可读（已解决）

采用**只读辅助命令 + 定向 NOPASSWD**，不保存 sudo 密码：

```
/usr/local/lib/power-monitor/rapl-read          root:root 0755
/etc/sudoers.d/power-monitor-rapl               root:root 0440
jimmy ALL=(root) NOPASSWD: /usr/local/lib/power-monitor/rapl-read
```

- 服务仍以 `jimmy` 身份运行，不获得完整 root 权限。
- 辅助命令只能读取 powercap 下的固定文件。
- 采集器优先直接读取；遇到 `EACCES` 时自动切换到 helper，并记录 `source=rapl-sudo:...`。
- 安装脚本：`sudo ./scripts/install-rapl-helper.sh`。
- 已验证两个 package 均能读出功率。

无论哪种方案，采集器在遇到 `EACCES` 时都必须返回 `unavailable` 并记录可操作的错误信息，
不能导致服务退出。

### B2. NVML 驱动/库版本不一致（已解决）

- 初检修复路径：维护窗口重启主机（DKMS 已就绪），或停止占用 GPU 的容器后卸载/重载 `nvidia*` 模块。
- 初检风险：`ollama` 等容器可能占用 GPU，重启/重载会中断其任务。
- 2026-11-18 已通过重启修复并真机验证：NVML 可用，2 张 V100 功率/温度/显存实测正常（见 §9）。
- 降级逻辑仍保留：NVML 初始化失败时 GPU 标记 `unavailable`，服务不崩。

### B3. 缺少 Python 依赖（已解决）

项目 `.venv` 已安装 Phase 1 依赖：`nvidia-ml-py`、`pytest`、`pytest-asyncio` 等。
后续阶段通过 `requirements.txt` / `requirements-dev.txt` 追加。

### B4. 未配置外部电表（预期）

无 Shelly / Modbus / PDU 设备信息。Phase 4 再接入；MVP 使用估算模式并明确标注。

## 7. 已确认的决策

| 编号 | 决策 | 结果 |
|---|---|---|
| D1 | 服务运行身份（RAPL 权限） | 以 `jimmy` 运行；只读 helper + `NOPASSWD`，不保存密码 |
| D2 | GPU 驱动修复时机 | 已重启修复；2026-11-18 真机验证 2 张 V100 正常 |
| D3 | `estimated_other_power_w` 默认值 | 默认留空，按 socket/内存/磁盘自动建议；写固定值则覆盖；均非实测 |
| D4 | Python 环境 | 项目内 `.venv`，`scripts/run.sh` 负责激活 |
| D5 | 监听地址/端口 | `0.0.0.0:8010` |

## 8. 复核命令（只读）

```bash
# RAPL 域与能量（需要 root 才能读 energy_uj）
ls /sys/class/powercap/
cat /sys/class/powercap/intel-rapl:0/name
sudo cat /sys/class/powercap/intel-rapl:0/energy_uj

# GPU 状态（驱动已修复，可随时只读查询）
nvidia-smi --query-gpu=uuid,name,power.draw,temperature.gpu,utilization.gpu --format=csv

# 温度/频率
cat /sys/class/hwmon/hwmon1/temp1_input
cat /sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq
```

## 9. 2026-11-18 复核（主机重启后）

主机重启后运行内核为 `6.8.0-149-generic`，`nvidia-smi` 与 NVML 报 `580.178.04 / CUDA 13.0`，
初检的 `Driver/library version mismatch` 已消失。`python -m app doctor` 结果：

| 项目 | 复核结果 |
|---|---|
| NVML | 初始化成功，枚举 2 张 `Tesla V100-SXM2-16GB` |
| GPU 功率 | `38.7 W`（index 0）、`35.8 W`（index 1），`quality=measured`，`source=nvml:<uuid>` |
| 功率上限 / 温度 | 均为 `300 W` / `32 °C`，与 `nvidia-smi` 一致 |
| 显存 | 各约 `239.9 MiB / 16 GiB`；利用率 `0 %`，时钟 `135 / 877 MHz` |
| 风扇 | `unavailable`（`Not Supported`），Tesla 预期行为 |
| 备用路径 | `nvidia-smi --query-gpu` 回退读数与 NVML 一致 |
| RAPL | 2 个 package 经只读 helper 测得 `2.9 W` / `4.7 W`（空闲），`source=rapl-sudo:...` |
| 采集器健康 | `/api/v1/system` 显示全部 7 个采集器 `state=ok` |
| 整机功率 | `estimated / dc_components`，`missing_components=[]`；能量 `estimated` |

结论：Phase 1 GPU 出口标准已满足，README/ARCHITECTURE 中“待驱动恢复”的标注已清除。
外部电表仍只有模拟设备端到端验证，真实硬件接入待定。
