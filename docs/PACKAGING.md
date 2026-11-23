# Ubuntu `.deb` 打包与安装

目标平台：**Ubuntu 24.04 LTS (noble) / amd64 / Python 3.12**。

## 构建

```bash
./scripts/package/build-deb.sh
```

产物：`dist/power-monitor_<version>_amd64.deb`。

构建脚本会：

1. 从 `pyproject.toml` 读取版本号（避免与 `app.__version__` 漂移）。
2. 用 `pip download --only-binary --platform manylinux2014_x86_64 --python-version 3.12`
   把 `scripts/package/requirements.lock.txt` 中的依赖下载到 `.cache/wheels/`（缓存复用）。
3. 组装 payload 后用 `dpkg-deb --build` 生成 `.deb`，并打印 SHA-256。

生成的包**自带 wheelhouse**，目标机器安装时不需要联网下载 Python 依赖。

## 安装

```bash
sudo apt install ./dist/power-monitor_0.1.0_amd64.deb
```

或者双击 `.deb` 由 Ubuntu 软件中心安装。安装结束后：

- Web 界面：`http://localhost:8010/`
- 硬件检测报告：`/var/lib/power-monitor/hardware-report.txt`
- 配置：`/etc/power-monitor/config.yaml`（dpkg `conffile`，升级时保留本地修改）
- 服务日志：`journalctl -u power-monitor -f`
- CLI：`power-monitor doctor` / `power-monitor check` / `power-monitor sample --count 5`

## apt 会自动执行哪些脚本？

会。`apt` 调用 `dpkg`，`dpkg` 在以下阶段自动执行包内 `DEBIAN/` 下的 maintainer scripts：

| 脚本 | 触发时机 | 本包中的动作 |
|---|---|---|
| `preinst` | 解包前 | 本包未使用 |
| `postinst` | 解包后 / 配置时 | 建系统用户与目录、离线创建 venv、校验 sudoers、跑硬件检测、启用并启动 systemd 服务、探测 `/health` |
| `prerm` | 卸载/升级前 | 停止并禁用服务 |
| `postrm` | 卸载/清除后 | `remove` 只重载 systemd；`purge` 额外删除 venv、数据目录、兼容软链和系统用户 |

## 脚本执行失败会怎样？

`postinst` 以非零状态退出会让**整个安装失败**：`apt` 会尝试回滚，包可能处于 `iF`（已解包但未配置）状态；可用
`sudo dpkg --configure -a` 重试，或 `sudo apt purge power-monitor` 清理。

因此本包区分两类步骤：

- **硬失败**（确实无法运行，直接退出非零）：创建系统用户/数据目录、创建 venv、离线安装依赖、
  导入核心模块校验、`visudo -cf` 校验 sudoers 规则。
- **非致命**（只告警，安装继续）：硬件检测结果、NVIDIA 驱动提示、服务启动失败、`/health` 未就绪。
  这样无 RAPL 的虚拟机、无 GPU 的机器、容器里没有 systemd 的环境也能装上并卸载。

`prerm` / `postrm` 中的清理命令多数带 `|| true`，避免卸载因为环境已变化而卡住。

## 驱动策略

只检测并提示，不自动安装闭源驱动：

- CPU RAPL、AMD hwmon、温度：内核自带；权限问题由只读 helper + 受限 sudoers 解决。
- NVIDIA：`nvidia-ml-py` 只是 Python 绑定，真正的 `libnvidia-ml.so` 来自 `nvidia-driver-*`。
  安装时若发现 NVIDIA PCI 设备但 NVML/`nvidia-smi` 不可用，会在终端和硬件报告里给出
  `sudo apt install nvidia-driver-XXX && sudo reboot` 提示，但不会中断安装，也不会擅自改内核驱动
  （DKMS、Secure Boot MOK、重启都需要用户决策）。
- 外部电表：不需要驱动。

## 运行时目录

`app.config.PM_DATA_DIR` 在开发模式下未设置，仍以项目根目录为基准；服务单元设置为
`/var/lib/power-monitor`，所以 `database.path: data/power_monitor.db` 落在
`/var/lib/power-monitor/data/power_monitor.db`，`/usr/lib/power-monitor` 保持只读。

RAPL helper 位于 `/usr/lib/power-monitor/rapl-read`，同时在
`/usr/local/lib/power-monitor/rapl-read` 放兼容软链，手工安装过旧版本的用户配置仍可工作。

## 限制与扩展

- Ubuntu 22.04 的 Python 3.10 不满足项目要求（`requires-python >=3.12`，代码使用 `StrEnum`）。
- amd64 与 arm64 的原生 wheel 不同，需要分别构建；当前脚本固定 amd64。
- 卸载保留数据：`sudo apt remove power-monitor`；彻底清除：`sudo apt purge power-monitor`。

## 从手工安装迁移到 .deb

手工安装的服务以你的登录用户运行，数据库在项目目录 `data/power_monitor.db`；`.deb` 改用专用系统用户
和 `/var/lib/power-monitor/data/power_monitor.db`，且**不会自动迁移历史数据**。如需保留历史，先停服务再拷贝：

```bash
sudo systemctl stop power-monitor
sudo mkdir -p /var/lib/power-monitor/data
sudo cp /home/<user>/code/power-monitor/data/power_monitor.db* /var/lib/power-monitor/data/
sudo chown -R power-monitor:power-monitor /var/lib/power-monitor
sudo apt install ./dist/power-monitor_0.1.0_amd64.deb
```

如果只是修改了项目代码并 `systemctl restart power-monitor`，则不受影响：现有 unit 未设置 `PM_DATA_DIR`，
数据库路径仍按项目根目录解析。
