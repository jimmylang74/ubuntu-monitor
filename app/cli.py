"""Command line diagnostics and sampler (Phase 1)."""

from __future__ import annotations

import argparse
import importlib.metadata
import shutil
import sys
import time
from pathlib import Path
from typing import Sequence

import psutil

from app import __version__
from app.collectors.cpu import CpuCollector, read_cpu_model, read_package_temperatures
from app.collectors.disk import DiskCollector
from app.collectors.gpu import GpuCollector
from app.collectors.memory import MemoryCollector
from app.collectors.network import NetworkCollector, filter_interfaces
from app.collectors.system import SystemCollector
from app.config import AppConfig, load_config
from app.domain import Measurement, Quality
from app.power.adapters.amdgpu import AmdgpuAdapter
from app.power.adapters.nvidia import NvidiaAdapter, NvidiaSmiAdapter
from app.power.adapters.rapl import RaplAdapter
from app.power.calculator import MeterState, compute_system_power, suggest_other_power_w


def _section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _fmt_power(value: Measurement[float], digits: int = 1) -> str:
    if value.value is None:
        reason = f": {value.error}" if value.error else ""
        return f"unavailable{reason}"
    suffix = "" if value.quality is Quality.MEASURED else f" [{value.quality.value}]"
    return f"{value.value:.{digits}f} {value.unit}{suffix}"


def _fmt_number(value: Measurement[float | int], digits: int = 1) -> str:
    if value.value is None:
        reason = f": {value.error}" if value.error else ""
        return f"unavailable{reason}"
    suffix = "" if value.quality is Quality.MEASURED else f" [{value.quality.value}]"
    return f"{value.value:.{digits}f} {value.unit}{suffix}"


def _human_bytes(value: int | None) -> str:
    if value is None:
        return "unavailable"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TiB"


def _direct_readable(path: Path) -> tuple[bool, str | None]:
    try:
        path.read_text(encoding="ascii")
        return True, None
    except OSError as exc:
        return False, str(exc)


def _pkg_version(name: str) -> str:
    try:
        return importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        return "not installed"


def doctor(cfg: AppConfig) -> int:
    print(f"Ubuntu Power Monitor {__version__}")
    print(f"config: {cfg.source_path or 'defaults (config/config.yaml not found)'}")
    print(f"python: {sys.version.split()[0]}  psutil: {_pkg_version('psutil')}  "
          f"pynvml: {_pkg_version('nvidia-ml-py')}  pydantic: {_pkg_version('pydantic')}")

    _section("CPU")
    print(f"model: {read_cpu_model()}")
    print(f"logical CPUs: {psutil.cpu_count()}  physical cores: {psutil.cpu_count(logical=False)}")
    freq = psutil.cpu_freq()
    if freq is not None:
        print(f"frequency: {freq.current:.0f} MHz (min {freq.min:.0f}, max {freq.max:.0f})")

    _section("RAPL / powercap")
    adapter = RaplAdapter(cfg.rapl)
    adapter.refresh()
    domains = adapter.all_domains
    if not domains:
        print("no RAPL domains found")
    else:
        for domain in domains:
            readable, error = _direct_readable(domain.path / "energy_uj")
            kind = "package" if domain.is_package else ("child" if domain.is_child else "domain")
            print(
                f"{domain.domain_id:20s} {domain.name:12s} {kind:8s} "
                f"enabled={domain.enabled} max_uj={domain.max_energy_uj} "
                f"direct_readable={readable}"
                + (f" ({error})" if error else "")
            )
        helper = Path(cfg.rapl.helper)
        print(f"helper: {helper} exists={helper.is_file()}  sudo={shutil.which('sudo') or 'missing'}")
        print("sampling twice to derive power ...")
        adapter.sample()
        time.sleep(1.1)
        for rapl_sample in adapter.sample():
            if rapl_sample.power_w is None:
                power_text = f"unavailable: {rapl_sample.error}" if rapl_sample.error else "unavailable"
            else:
                power_text = f"{rapl_sample.power_w:.1f} W"
            energy_text = f"{rapl_sample.energy_j:.0f} J" if rapl_sample.energy_j is not None else "unavailable"
            print(
                f"{rapl_sample.name:12s} power={power_text}  energy={energy_text}  "
                f"source={rapl_sample.source}"
            )

    _section("NVIDIA GPU")
    nvml = NvidiaAdapter(cfg.gpu)
    if nvml.ensure_initialized():
        infos = nvml.gpus
        print(f"NVML initialized, {len(infos)} GPU(s)")
        for info in infos:
            print(f"  {info.uuid}  index={info.index}  {info.name}  pci={info.pci_bus_id}")
        for gpu in nvml.sample():
            print(
                f"  {gpu.uuid} power={_fmt_power(gpu.power)} limit={_fmt_power(gpu.power_limit)} "
                f"temp={_fmt_number(gpu.temperature)} util={_fmt_number(gpu.utilization)} "
                f"memory={_human_bytes(gpu.memory_used.value)}/{_human_bytes(gpu.memory_total.value)} "
                f"fan={_fmt_number(gpu.fan_speed)}"
            )
        nvml.close()
    else:
        print(f"NVML unavailable: {nvml.init_error}")
    smi = NvidiaSmiAdapter(cfg.gpu)
    print(f"nvidia-smi: {shutil.which('nvidia-smi') or 'not found'}")
    smi_gpus = smi.sample()
    if smi_gpus:
        for gpu in smi_gpus:
            print(f"  fallback {gpu.uuid} power={_fmt_power(gpu.power)}")
    elif smi.error:
        print(f"nvidia-smi unavailable: {smi.error}")

    _section("AMD GPU (amdgpu hwmon)")
    amd = AmdgpuAdapter(cfg.gpu)
    amd_gpus = amd.sample()
    if amd_gpus:
        for gpu in amd_gpus:
            note = "" if gpu.include_in_system_total else "  [APU: CPU power already includes GPU]"
            print(
                f"  {gpu.uuid} {gpu.name} power={_fmt_power(gpu.power)} "
                f"limit={_fmt_power(gpu.power_limit)} temp={_fmt_number(gpu.temperature)} "
                f"util={_fmt_number(gpu.utilization)} "
                f"vram={_human_bytes(gpu.memory_used.value)}/{_human_bytes(gpu.memory_total.value)}"
                f"{note}"
            )
    else:
        print("no AMD GPU hwmon node found")

    _section("Temperatures (hwmon coretemp / k10temp)")
    temperatures = read_package_temperatures()
    if temperatures:
        for package_id, value in sorted(temperatures.items()):
            print(f"package-{package_id}: {value:.1f} °C")
    else:
        print("no CPU package temperature found")

    _section("Memory / disks / network")
    memory = psutil.virtual_memory()
    print(
        f"memory: {_human_bytes(memory.used)} / {_human_bytes(memory.total)} "
        f"({memory.percent:.1f}%), available {_human_bytes(memory.available)}"
    )
    for disk in psutil.disk_partitions(all=False):
        try:
            usage = psutil.disk_usage(disk.mountpoint)
        except OSError:
            continue
        if usage.total == 0 or disk.fstype == "squashfs":
            continue
        print(
            f"disk {disk.mountpoint:20s} {disk.device:20s} "
            f"{_human_bytes(usage.used)} / {_human_bytes(usage.total)} ({usage.percent:.1f}%)"
        )
    io = psutil.net_io_counters(pernic=True) or {}
    selected = filter_interfaces(io, cfg.network)
    print(f"network interfaces (after filter): {', '.join(selected) if selected else 'none'}")
    print(f"all interfaces: {', '.join(sorted(io))}")

    _section("Configuration")
    print(f"server: {cfg.server.host}:{cfg.server.port}")
    print(
        f"intervals: cpu={cfg.collector.cpu_interval}s gpu={cfg.collector.gpu_interval}s "
        f"memory={cfg.collector.memory_interval}s disk={cfg.collector.disk_interval}s "
        f"network={cfg.collector.network_interval}s"
    )
    other = (
        "auto" if cfg.power.estimated_other_power_w is None else f"{cfg.power.estimated_other_power_w} W"
    )
    print(
        f"power: mode={cfg.power.mode} other_components={other} "
        f"strict_estimate={cfg.power.strict_estimate}"
    )
    print(f"electricity: enabled={cfg.electricity.enabled} price={cfg.electricity.price_per_kwh} "
          f"{cfg.electricity.currency}")
    return 0


def check(cfg: AppConfig) -> int:
    """Validate and print the effective configuration without hardware access."""
    print(f"configuration OK: {cfg.source_path or 'defaults (config/config.yaml not found)'}")
    print(f"server: {cfg.server.host}:{cfg.server.port} log_level={cfg.server.log_level} "
          f"log_file={cfg.server.log_file or 'stderr/journal'}")
    print(f"database: {cfg.database_path} write_interval={cfg.database.write_interval}s "
          f"queue_size={cfg.database.queue_size}")
    print(f"retention: raw={cfg.history.raw_retention_days}d "
          f"1m={cfg.history.minute_retention_days}d "
          f"1h={cfg.history.hourly_retention_days}d cleanup_hour={cfg.history.cleanup_hour}")
    meter = cfg.power.external_meter
    print(f"external meter: enabled={meter.enabled} type={meter.type} "
          f"interval={meter.interval}s failover_after={meter.failover_after}")
    other = (
        "auto" if cfg.power.estimated_other_power_w is None else f"{cfg.power.estimated_other_power_w} W"
    )
    print(f"power: mode={cfg.power.mode} strict_estimate={cfg.power.strict_estimate} "
          f"other_components={other}")
    return 0


def _print_sample(
    sequence: int,
    cfg: AppConfig,
    cpu_collector: CpuCollector,
    gpu_collector: GpuCollector,
    memory_collector: MemoryCollector,
    disk_collector: DiskCollector,
    network_collector: NetworkCollector,
    *,
    show_memory: bool,
    show_disk: bool,
) -> None:
    cpu = cpu_collector.collect()
    gpus = gpu_collector.collect()
    networks = network_collector.collect()
    # Always refresh memory/disk inventory for the auto other-power suggestion;
    # only the detailed printout follows the display throttling.
    memory = memory_collector.collect()
    disks = disk_collector.collect()
    displayed_memory = memory if show_memory else None
    displayed_disks = disks if show_disk else ()

    other_w = cfg.power.estimated_other_power_w
    other_source: str | None = None
    if other_w is None:
        disk_names = tuple(sorted({Path(d.device).name for d in disks if d.device}))
        suggestion = suggest_other_power_w(
            sockets=cpu.socket_count,
            memory_bytes=int(memory.total.value) if memory and memory.total.value is not None else None,
            disk_devices=disk_names,
            gpu_count=len(gpus),
        )
        other_w = suggestion.watts
        other_source = f"auto:{suggestion.basis}"

    meter_state = MeterState(enabled=cfg.power.external_meter.enabled, healthy=False)
    system = compute_system_power(
        cpu,
        gpus,
        None,
        meter_state,
        cfg.power,
        cpu.timestamp,
        gpu_available=gpu_collector.available,
        other_components_w=other_w,
        other_power_source=other_source,
    )

    stamp = cpu.timestamp.astimezone().strftime("%H:%M:%S")
    scope = f" ({system.scope})" if system.scope else ""
    meter_note = ""
    if system.meter_status == "offline":
        meter_note = "  [meter offline]"
    elif system.meter_status == "disabled":
        meter_note = "  [meter disabled]"
    other_label = "auto" if system.other_power_source.startswith("auto:") else "config"
    print(
        f"[{stamp}] #{sequence} System {_fmt_power(system.power)}"
        f"{scope}{meter_note}  (other components {other_label}: {system.other_components_w:.0f} W)"
    )

    gpu_power = ", ".join(_fmt_power(g.power) for g in gpus) if gpus else "none"
    print(
        f"  CPU {_fmt_power(cpu.total_power)}  usage {_fmt_number(cpu.usage_percent)}  "
        f"max {_fmt_number(cpu.max_temperature)}  |  GPU {gpu_power}"
    )
    for package in cpu.packages:
        print(
            f"    {package.name}  {_fmt_power(package.power)}  "
            f"{_fmt_number(package.temperature)}  usage {_fmt_number(package.usage_percent)}  "
            f"{_fmt_number(package.frequency_mhz, 0)}  {package.core_count}C/{package.thread_count}T  "
            f"energy {_fmt_number(package.energy_j, 0)}"
        )
    if gpu_collector.error:
        print(f"    GPU unavailable: {gpu_collector.error}")
    for gpu in gpus:
        print(
            f"    {gpu.name} [{gpu.uuid}]  {_fmt_power(gpu.power)}  limit {_fmt_power(gpu.power_limit)}  "
            f"{_fmt_number(gpu.temperature)}  util {_fmt_number(gpu.utilization)}  "
            f"mem {_human_bytes(gpu.memory_used.value)}/{_human_bytes(gpu.memory_total.value)}  "
            f"fan {_fmt_number(gpu.fan_speed)}"
        )
    if displayed_memory is not None:
        print(
            f"    MEM {_fmt_number(displayed_memory.usage_percent)}  "
            f"{_human_bytes(displayed_memory.used.value)}/{_human_bytes(displayed_memory.total.value)}  "
            f"swap {_human_bytes(displayed_memory.swap_used.value)}/"
            f"{_human_bytes(displayed_memory.swap_total.value)}"
        )
    if networks:
        net_text = "  ".join(
            f"{n.name} rx {_fmt_number(n.rx_mbps)} tx {_fmt_number(n.tx_mbps)}" for n in networks
        )
        print(f"    NET {net_text}")
    for disk in displayed_disks:
        print(
            f"    DISK {disk.device} @ {disk.mountpoint}  "
            f"{_human_bytes(disk.used.value)}/{_human_bytes(disk.total.value)} "
            f"({_fmt_number(disk.usage_percent)})  "
            f"r {_fmt_number(disk.read_mbps)} w {_fmt_number(disk.write_mbps)}  "
            f"iops r {_fmt_number(disk.read_iops, 0)} w {_fmt_number(disk.write_iops, 0)}"
        )


def sample(cfg: AppConfig, count: int, interval: float) -> int:
    cpu_collector = CpuCollector(cfg)
    gpu_collector = GpuCollector(cfg)
    memory_collector = MemoryCollector(cfg)
    disk_collector = DiskCollector(cfg)
    network_collector = NetworkCollector(cfg)
    system_collector = SystemCollector(cfg)

    print(f"sampling {count} time(s) at {interval:.1f}s interval; Ctrl-C to stop")
    started = time.monotonic()
    next_memory = started
    next_disk = started
    try:
        for sequence in range(1, count + 1):
            loop_started = time.monotonic()
            if sequence == 1:
                info = system_collector.collect()
                print(
                    f"host {info['hostname']}  kernel {info['kernel']}  "
                    f"python {info['python']}  collected {info['collected_at']}"
                )
            show_memory = loop_started >= next_memory
            show_disk = loop_started >= next_disk
            _print_sample(
                sequence,
                cfg,
                cpu_collector,
                gpu_collector,
                memory_collector,
                disk_collector,
                network_collector,
                show_memory=show_memory,
                show_disk=show_disk,
            )
            if show_memory:
                next_memory = loop_started + cfg.collector.memory_interval
            if show_disk:
                next_disk = loop_started + cfg.collector.disk_interval
            elapsed = time.monotonic() - loop_started
            if sequence < count:
                time.sleep(max(0.0, interval - elapsed))
    except KeyboardInterrupt:
        print("\ninterrupted")
    finally:
        gpu_collector.close()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="power-monitor", description="Ubuntu Power Monitor")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", type=Path, default=None, help="path to config.yaml")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("check", help="validate configuration and print effective settings")
    sub.add_parser("doctor", help="inspect CPU/RAPL/GPU/hwmon/psutil environment")

    sample_parser = sub.add_parser("sample", help="collect and print live samples")
    sample_parser.add_argument("--count", type=int, default=5)
    sample_parser.add_argument("--interval", type=float, default=1.0)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 2
    if getattr(args, "count", 1) < 1:
        parser.error("--count must be >= 1")
    if getattr(args, "interval", 1.0) <= 0:
        parser.error("--interval must be > 0")

    try:
        cfg = load_config(args.config)
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2

    if args.command == "check":
        return check(cfg)
    if args.command == "doctor":
        return doctor(cfg)
    if args.command == "sample":
        return sample(cfg, args.count, args.interval)
    parser.error(f"unknown command {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
