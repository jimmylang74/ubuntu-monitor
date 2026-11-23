# Ubuntu Power Monitor

Python 3.12 + FastAPI 的服务器功耗监控系统。当前处于 **Phase 5：部署与优化（已完成）**。

## 当前状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 0 | 环境检查、架构设计 | ✅ 完成 |
| Phase 1 | 硬件采集核心（RAPL / NVML / psutil + CLI + 单测） | ✅ 完成（RAPL / GPU / CPU 真机验证通过） |
| Phase 2 | FastAPI / WebSocket / SQLite / 后台采集服务 | ✅ 完成 |
| Phase 3 | Web 前端 | ✅ 完成（本机桌面/手机验证通过，含真实 GPU） |
| Phase 4 | 外部电表 Adapter（Shelly Gen1/Gen2、Modbus、HTTP PDU） | ✅ 完成（模拟电表端到端验证；真实电表待接入） |
| Phase 5 | systemd / 日志轮转 / 备份与清理 / 稳定性检查 | ✅ 完成（systemd 与真实 RAPL 验证通过） |

## 文档

- [环境检查报告](docs/ENVIRONMENT.md)
- [架构设计](docs/ARCHITECTURE.md)
- [配置示例](config/config.example.yaml)

## 硬件支持

| 部件 | 支持情况 |
|---|---|
| Intel CPU 功率/能耗 | RAPL（`/sys/class/powercap/intel-rapl:*`），权限不足时用只读 helper |
| AMD Zen CPU 功率/能耗 | Linux 主线同样注册为 `intel-rapl:*`（Zen 1–5，取决于内核版本），代码无需按厂商分支 |
| CPU 温度 | Intel `coretemp` 与 AMD `k10temp`（`Tctl`/`Tdie`） |
| NVIDIA GPU | NVML，失败时回退 `nvidia-smi` |
| AMD GPU | `amdgpu`/`radeon` hwmon：功率、上限、温度、利用率、显存、时钟、风扇 |
| 混合 NVIDIA + AMD GPU | 两个后端独立枚举；`gpu.backends` 可单独启用/关闭 |
| 整机实测功率 | 外部电表（Shelly / Modbus / HTTP PDU），与 CPU/GPU 厂商无关 |

APU 的 hwmon 功率通常已包含 CPU package，默认会从整机分项求和中排除，但仍会展示数据；
可用 `gpu.amdgpu.apu_policy` 覆盖。未直接测量的主板/内存/风扇/磁盘/网卡功率默认按检测到的
硬件自动给出建议值，写入 `power.estimated_other_power_w` 可固定为你自己的数值。

## 快速开始

```bash
# 1. 创建项目内 .venv 并安装依赖
./scripts/setup.sh

# 2. 安装只读 RAPL 辅助命令（一次性，需要 sudo）
sudo ./scripts/install-rapl-helper.sh

# 3. 环境诊断
./scripts/run.sh doctor

# 4. 连续采样（Phase 1 CLI）
./scripts/run.sh sample --count 10 --interval 1

# 5. 校验配置（不访问硬件）
./scripts/run.sh check

# 6. 安装并启动 systemd 服务（推荐，开机自启）
sudo ./scripts/install-service.sh

# 或前台运行（调试）
./scripts/run.sh

# 或使用显式配置
./scripts/run.sh --config config/config.yaml
```

`scripts/run.sh` 会激活项目内 `.venv` 后运行程序。服务默认监听 `0.0.0.0:8010`。
编辑 `config/config.yaml` 修改端口、采集周期、`power.estimated_other_power_w` 等配置；
该值留空时会自动给出建议值。`config/config.example.yaml` 是带注释的完整示例。

服务启动后：

- Web 界面：<http://localhost:8010/>（总览 / CPU / GPU / 能耗）
- 服务元数据：<http://localhost:8010/api>
- 交互式 API 文档：<http://localhost:8010/docs>
- 健康检查：<http://localhost:8010/health>
- 实时数据：`ws://localhost:8010/ws/realtime`

## 部署（systemd）

`deploy/power-monitor.service` 是 unit 模板，`scripts/install-service.sh` 会按当前路径与用户
生成 `/etc/systemd/system/power-monitor.service` 并启动：

```bash
sudo ./scripts/install-service.sh
systemctl status power-monitor
journalctl -u power-monitor -f
```

- 服务以运行安装脚本的用户（默认 `SUDO_USER`，可用 `PM_USER=` 覆盖）身份运行，不使用 root。
- `PM_CONFIG` 指向 `config/config.yaml`；修改配置后执行 `sudo systemctl restart power-monitor`。
- `Restart=on-failure` + `RestartSec=5s`；进程被杀死或异常退出后自动恢复。
- RAPL 通过 `scripts/install-rapl-helper.sh` 安装的只读 helper + 作用域内 `NOPASSWD` 规则读取；
  unit 不保存密码，也没有启用 `NoNewPrivileges`（否则 `sudo` 无法执行 helper）。
- 日志默认写入 journald（`journalctl` 自带轮转）；也可在 `server.log_file` 指定文件，
  使用 `log_max_bytes` / `log_backup_count` 做大小轮转。若使用文件日志，请同步把该目录加入 unit 的
  `ReadWritePaths`。
- 默认只允许写仓库下的 `data/` 与 `logs/`；若把 `database.path` 或 `server.log_file`
  指向其他目录，需要把该目录加入 unit 的 `ReadWritePaths`。

## REST 与 WebSocket

| 端点 | 说明 |
|---|---|
| `GET /`、`/cpu`、`/gpu`、`/energy` | Web UI 页面（Jinja2 模板 + 原生 ES modules） |
| `GET /api` | 服务名、版本、API 与 WebSocket 入口 |
| `GET /health` | 进程运行时间、采集器、数据库与外部电表状态；异常时返回 `degraded` |
| `GET /api/v1/system` | 主机、GPU 可用性与最新快照时间 |
| `GET /api/v1/cpu` | 每 package 功率/能量/温度/频率/使用率 |
| `GET /api/v1/gpu` | 每 GPU 功率、温度、显存、风扇、时钟；枚举失败时 `available=false` |
| `GET /api/v1/power/current` | 系统功率、CPU/GPU 功率、区间能量与 `meter` 状态，含 `quality/scope/source/missing_components` |
| `GET /api/v1/meter` | 外部电表状态、能力、最后读数、最后成功时间与连续失败次数 |
| `GET /api/v1/power/history?window=5m&series=system,cpu` | 历史曲线，服务端降采样 |
| `GET /api/v1/energy/daily?days=7` | 按本地时区切分的每日能耗与费用 |
| `GET /api/v1/energy/weekly` / `monthly` / `summary` | 固定窗口与今日/昨日/7 天/30 天汇总 |
| `GET /api/v1/config` | 当前生效配置（只读，不含敏感信息） |
| `GET /api/v1/diagnostics` | RAPL 域/权限、NVML/AMD GPU 状态、采集器健康、表行数 |
| `WS /ws/realtime` | 连接后收到 `hello` + 完整快照，之后每秒推送；20 s 无数据发 `ping` |

历史窗口：`5m`、`15m`、`1h` 读取原始样本；`6h`、`24h` 读取 1 分钟聚合（无聚合时回退原始样本）；
`7d`、`30d`、`365d` 读取 1 小时聚合。单条序列最多返回 `history.max_points` 个点（默认 2000）。
单 GPU 序列使用 `series=gpu:<uuid>`，只支持 24 h 及更短窗口。

WebSocket 每次推送完整快照；服务端为每个连接维护容量为 1 的队列，慢客户端只会跳过中间帧，
不会拖慢采集。客户端断开后可直接重连，服务端会重新发送 `hello` 与最新快照。

## 数据持久化

- SQLite 文件：`data/power_monitor.db`（`WAL`、`synchronous=NORMAL`、`busy_timeout=5000`）。
- 原始样本按 `database.write_interval` 写入（默认 1 s）；采集频率不受写库影响。
- 单写者后台任务：写队列默认容量 300，满时丢弃最旧样本并计数，绝不阻塞采集或 WebSocket。
- 聚合：每分钟生成 1 分钟桶，每小时生成 1 小时桶；聚合幂等，重启后会自动补齐最近窗口。
- 默认保留：原始样本 7 天、1 分钟聚合 90 天、1 小时聚合 365 天；每天 `history.cleanup_hour` 清理。
- 数据库不可用时：`/health` 显示 `degraded`，历史/能耗接口返回 `503`，采集和 WebSocket 继续工作；
  磁盘恢复后自动重连并继续写入。

### 备份与恢复

```bash
# 在线备份（使用 sqlite3 backup API，不停止服务），默认保留最近 14 份
./scripts/backup.sh

# 指定目标与保留数量
./scripts/backup.sh --dest /mnt/backups/power-monitor.db --keep 30
```

备份完成后会执行 `PRAGMA integrity_check` 并打印各表行数，默认写入 `backups/`（已在 `.gitignore`）。

恢复步骤：

```bash
sudo systemctl stop power-monitor
cp backups/power-monitor-YYYYmmdd-HHMMSS.db data/power_monitor.db
sudo systemctl start power-monitor
```

清理由 `RetentionService` 每天在 `history.cleanup_hour` 自动执行（分批删除，避免长事务）：
原始样本 7 天、1 分钟聚合 90 天、1 小时聚合 365 天。备份不会自动替代清理策略。

### 稳定性检查

```bash
# 默认监控 1 小时；短时验证可传 --duration
.venv/bin/python scripts/stability_check.py --duration 3600 --interval 10
.venv/bin/python scripts/stability_check.py --duration 60 --interval 5
```

脚本自动找到监听 8010 的进程，持续采样 RSS、数据库体积、`/health` 与
`/api/v1/power/current`，检查进程存活、HTTP 失败、RSS 增长（默认 ≤64 MiB）与数据库增长
（默认 ≤512 MiB），最后输出 `RESULT: PASS/FAIL` 并以退出码表示结果。
前端缓冲区上限由 Playwright 检查覆盖，见 `docs/ARCHITECTURE.md`。

## 外部电表（Phase 4）

在 `config/config.yaml` 中启用（默认关闭）：

```yaml
power:
  external_meter:
    enabled: true
    type: shelly          # shelly | modbus | http_pdu
    interval: 1.0         # 轮询间隔（秒）
    failover_after: 3     # 连续失败多少次后退回估算
    shelly:
      url: ""             # 换成你的电表地址
      generation: auto    # auto | gen1 | gen2
```

- `shelly`：自动探测 Gen2（`/rpc/Shelly.GetStatus`）或 Gen1（`/status`）；Gen2 的
  `aenergy.total`（Wh）转换为 kWh。Gen1 的 `total` 单位在不同固件间存在歧义，因此不猜测为 kWh。
- `modbus`：`pymodbus` TCP，支持 uint32/float32、大小字序与缩放；只读取一个 32 位功率寄存器。
- `http_pdu`：通用只读 JSON 接口，用点分路径映射 `power_path` / `energy_path` 等字段。
- 电表健康时系统功率为 `quality=measured`、`scope=wall`，并优先使用电表累计电量差值；
  计数器回退或异常跳变（>5000 Wh）时丢弃差值、回退到功率积分，绝不编造电量。
- 连续失败达到 `failover_after` 后自动退回 `estimated / dc_components`，保留最后成功读数与时间；
  恢复后自动切回实测并写入事件日志（`event_log` 的 `component=meter`）。
- `GET /api/v1/meter` 与 WebSocket 快照的 `meter` 字段暴露状态；总览页显示电表功率、累计电能、
  电能来源、最后成功时间与失败计数，离线时显示告警。能耗页实时条会标注 `wall` 实测与回退状态。

## Phase 1 验证结果（本机）

| 项目 | 结果 |
|---|---|
| Python 环境 | 项目内 `.venv`，Python 3.12.3 |
| 单元测试 | 全量 `163 passed`（Phase 1–5） |
| RAPL | 2 个 package，经只读 helper 成功读取；空闲约 `5–14 W/package` |
| 温度 | `package-0` / `package-1` 经 hwmon coretemp 读取 |
| CPU 拓扑 | 2 socket、22C/44T per socket、88 线程 |
| GPU | 重启后 NVML 与 `nvidia-smi` 均正常：2 × Tesla V100 SXM2 16GB，约 `35–39 W/卡`、`31–32 °C`、`300 W` 上限；Tesla 风扇按预期为 `unavailable` |
| 整机估算 | 组件齐全时为 `estimated / dc_components`、`missing_components=[]`；缺失 GPU 时仍按严格规则返回 `unavailable`，不把缺失组件当作 0 W |

## Phase 2 验证结果（本机）

| 项目 | 结果 |
|---|---|
| 测试 | `115 passed`（Phase 3 完成后 `129 passed`；Phase 4 完成后全量 `163 passed`） |
| 实时写入 | 实际服务连续 10 s 写入 10 条原始样本（1 Hz） |
| 5 m 历史 | `GET /api/v1/power/history?window=5m` 返回按秒聚合的 system/cpu/gpu 曲线 |
| 重启持久化 | 重启后最旧样本仍在，历史查询连续 |
| WebSocket | 连续两次连接均收到 `hello` 与 `snapshot` |
| 数据库故障 | 指向不可打开路径时 `/health` 为 `degraded`，历史接口 `503`，WebSocket 仍正常推送 |
| GPU 故障 | 驱动不可用时系统功率与能量为 `unavailable`，`missing_components=["gpu"]`，进程不崩溃 |

## Phase 3 验证结果（本机）

| 项目 | 结果 |
|---|---|
| 测试 | 当时全量 `129 passed`；Phase 4 完成后全量 `163 passed`（新增页面、静态资源、无 CDN、ECharts 许可证与 `/api` 元数据测试） |
| 页面 | `/`、`/cpu`、`/gpu`、`/energy` 均返回 200，共享导航与连接状态 |
| 前端依赖 | ECharts 5.5.1 本地 vendored（`app/static/vendor/`，含 Apache-2.0 许可证），无 CDN 请求 |
| 实时连接 | Playwright 验证四个页面均进入 `Live`，无 console/page error |
| 真实 GPU | 驱动恢复后总览与 GPU 页显示 2 张 V100 的实测功率/温度/显存，徽标为 `Measured`；风扇按预期为 `Unavailable` |
| 时间范围 | 5 m 走 WS；15 m / 24 h 切换后正确重新拉取历史并更新图表元数据 |
| 重连 | 浏览器离线显示 `Offline`；恢复在线后立即重连并回到 `Live` |
| 响应式 | 1440 px 桌面与 390 px 手机均无横向溢出，导航与图表正常 |
| Tooltip | 质量徽标 tooltip 展示 `source`、`time`、`scope`，不依赖 HTML 注入 |
| 数据缺失 | GPU 不可用时徽标为 `Unavailable`，数值为 `—`，不显示 0 W |

## Phase 4 验证结果（本机）

| 项目 | 结果 |
|---|---|
| 测试 | 全量 `163 passed`（新增适配器解析、MeterService 切换/恢复、电表 API、聚合与历史 scope 测试） |
| Shelly | `httpx.MockTransport` 覆盖 Gen2 `switch:0`、Gen1 `meters[0]`、自动回退、无效 JSON 与 HTTP 错误 |
| Modbus | 覆盖 uint32/float32、大小字序、缩放、NaN、错误响应、`device_id`/旧版 `slave` 兼容与断线重连 |
| HTTP PDU | 覆盖点分路径、数组下标、缩放、缺失功率报错、可选字段 `unavailable` |
| 端到端（模拟电表） | 本地 mock Shelly Gen2 提供 420 W / 递增累计电量：系统功率 `measured/wall`，`meter_sample` 与 `sample.meter_w` 正常写入，累计差值约 1 Wh/s |
| 故障切换 | 停止 mock 后连续 3 次失败转 `offline`，系统回退估算并保留最后读数/时间；`event_log` 写入 warning；恢复后写 info 并回到 `ok` |
| UI | 1440 px 与 390 px 均无横向溢出、无 console/page error；总览显示电表状态/功率/累计电能/来源/最后成功/失败次数，离线时告警可见；能耗页实时条显示 `墙插实测` 与电能来源 |
| 默认关闭 | 未启用时 `/api/v1/meter` 为 `disabled`，不影响 CPU/GPU 采集与 UI |

## Phase 5 验证结果（本机）

| 项目 | 结果 |
|---|---|
| 测试 | 全量 `163 passed`（新增日志文件轮转与 `check` 命令测试） |
| systemd | `power-monitor.service` 安装并启用，`active (running)`；开机自启 |
| 运行身份 | 服务以 `jimmy` 运行；journal 中可见 `sudo ... /usr/local/lib/power-monitor/rapl-read`，CPU 功率 `measured` |
| 异常恢复 | 对主进程执行 `SIGKILL` 后，systemd 在 5 s 内拉起新 PID，`/health` 恢复 200 |
| 日志 | 默认写 journald；`server.log_file` 路径使用 `RotatingFileHandler`，单测验证写入与大小上限 |
| 配置校验 | `./scripts/run.sh check` 输出来源、监听地址、数据库、保留策略、电表与估算参数 |
| 备份 | `./scripts/backup.sh` 在线备份 37 MiB 数据库，`integrity_check=ok`，各表行数正常 |
| 稳定性 | 30 s 加速检查：RSS `125.2 → 126.0 MiB`（+0.8），数据库体积稳定，HTTP 失败 0，`RESULT: PASS` |
| 清理 | `RetentionService` 每日按配置分批清理；备份脚本默认保留 14 份 |

## RAPL 权限说明

`/sys/class/powercap/intel-rapl:*/energy_uj` 默认仅 root 可读。本项目**不保存 sudo 密码**，而是安装一个
只读辅助命令 `/usr/local/lib/power-monitor/rapl-read`，并授予当前用户 `NOPASSWD` 执行权限。
服务仍以普通用户身份运行。

## 本机阻塞项状态

原 NVIDIA 驱动/库版本不一致问题（`580.173.02` 内核模块 vs `580.178` 用户态 NVML）已通过主机重启解决：
新内核模块 `580.178.04` 已加载，`nvidia-smi` 与 NVML 均可用，2 张 V100 的功率/温度/显存已真机验证。

剩余待接入项（非阻塞）：真实外部电表的端到端验证仍是模拟电表结果，接入硬件后需复测故障切换与恢复。

## 数据可信度

所有功耗值包含 `value / unit / quality / source / timestamp`：

- `measured`：直接来自 RAPL、NVML、AMD hwmon、psutil 或外部电表
- `estimated`：实测组件求和 + 自动建议值或 `power.estimated_other_power_w`；`other_power_source` 标明来源
- `unavailable`：无法得出，值为 `null`，绝不用 0 代替。

系统功率额外携带 `scope`：`estimated` 的 `scope=dc_components`（DC 组件之和，不含 PSU 损耗）；
只有外部电表读数才是 `scope=wall` 的实测墙插功率。两者不可直接比较。
