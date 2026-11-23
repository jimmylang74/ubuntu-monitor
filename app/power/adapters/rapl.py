"""RAPL / powercap reader with wrap-around handling and multi-package support."""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable

from app.config import RaplConfig

DEFAULT_RAPL_ROOT = Path("/sys/class/powercap")


class RaplError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RaplDomain:
    domain_id: str
    name: str
    path: Path
    max_energy_uj: int
    enabled: bool
    is_child: bool
    is_package: bool


@dataclass(frozen=True, slots=True)
class RaplSample:
    domain_id: str
    name: str
    energy_uj: int | None
    energy_j: float | None
    power_w: float | None
    timestamp: datetime
    source: str
    error: str | None = None


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="ascii", errors="replace").strip()
    except OSError:
        return None


class RaplAdapter:
    """Read Intel/AMD RAPL powercap counters and derive watts.

    Only top-level ``package-*`` domains are considered CPU packages.  Child
    domains (``dram``, ``core``, ...) are exposed for diagnostics but never
    included in totals, avoiding parent/child double counting.
    """

    def __init__(
        self,
        cfg: RaplConfig,
        *,
        root: Path = DEFAULT_RAPL_ROOT,
        clock: Callable[[], float] = time.monotonic,
        now: Callable[[], datetime] | None = None,
        runner: Callable[[], str] | None = None,
    ) -> None:
        self.cfg = cfg
        self.root = root
        self._clock = clock
        self._now = now or (lambda: datetime.now(UTC))
        self._runner = runner or self._run_helper
        self._domains: dict[str, RaplDomain] = {}
        self._baseline: dict[str, tuple[int, float]] = {}
        self._energy_j: dict[str, float] = {}
        self._last_power: dict[str, float | None] = {}
        self._source_mode = "helper" if cfg.use_sudo == "always" else "direct"
        self._helper_error: str | None = None

    # ------------------------------------------------------------------ discovery

    @property
    def package_domains(self) -> list[RaplDomain]:
        return sorted(
            (d for d in self._domains.values() if d.is_package),
            key=lambda d: (d.name, d.domain_id),
        )

    @property
    def all_domains(self) -> list[RaplDomain]:
        return sorted(self._domains.values(), key=lambda d: d.domain_id)

    @property
    def source_mode(self) -> str:
        return self._source_mode

    @property
    def helper_error(self) -> str | None:
        return self._helper_error

    def refresh(self) -> None:
        domains: dict[str, RaplDomain] = {}
        if self.root.is_dir():
            paths: dict[str, Path] = {}
            for control_type in self.cfg.control_types:
                for path in self.root.glob(f"{control_type}:*"):
                    if path.is_dir():
                        paths[path.name] = path
            for domain_id, path in sorted(paths.items()):
                name = _read_text(path / "name") or domain_id
                max_raw = _read_text(path / "max_energy_range_uj")
                try:
                    max_uj = int(max_raw) if max_raw else 0
                except ValueError:
                    max_uj = 0
                enabled_raw = _read_text(path / "enabled")
                enabled = enabled_raw is None or enabled_raw == "1"
                is_child = domain_id.count(":") > 1
                domains[domain_id] = RaplDomain(
                    domain_id=domain_id,
                    name=name,
                    path=path,
                    max_energy_uj=max_uj,
                    enabled=enabled,
                    is_child=is_child,
                    is_package=name.startswith("package-") and not is_child,
                )

        removed = set(self._baseline) - set(domains)
        for domain_id in removed:
            self._baseline.pop(domain_id, None)
            self._energy_j.pop(domain_id, None)
            self._last_power.pop(domain_id, None)

        self._domains = domains

    # ------------------------------------------------------------------ energy IO

    def _run_helper(self) -> str:
        helper = Path(self.cfg.helper)
        if not helper.is_file():
            raise RaplError(f"RAPL helper not found: {helper}")
        try:
            proc = subprocess.run(
                ["sudo", "-n", str(helper)],
                capture_output=True,
                text=True,
                timeout=2.0,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RaplError(f"RAPL helper failed: {exc}") from exc
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout).strip()
            raise RaplError(f"RAPL helper exit {proc.returncode}: {detail}")
        return proc.stdout

    def _read_helper_energies(self) -> dict[str, int]:
        output = self._runner()
        energies: dict[str, int] = {}
        for line in output.splitlines():
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            domain_id, _name, raw_energy = parts[0], parts[1], parts[2]
            if not raw_energy:
                continue
            try:
                energies[domain_id] = int(raw_energy)
            except ValueError:
                continue
        return energies

    def _read_direct_energies(self) -> tuple[dict[str, int], dict[str, str], bool]:
        energies: dict[str, int] = {}
        errors: dict[str, str] = {}
        permission_denied = False
        for domain in self._domains.values():
            try:
                raw = (domain.path / "energy_uj").read_text(encoding="ascii").strip()
                energies[domain.domain_id] = int(raw)
            except PermissionError:
                permission_denied = True
                errors[domain.domain_id] = (
                    "permission denied reading energy_uj; run with CAP_DAC_READ_SEARCH, "
                    "root, or install the RAPL helper"
                )
            except (OSError, ValueError) as exc:
                errors[domain.domain_id] = f"cannot read energy_uj: {exc}"
        return energies, errors, permission_denied

    def _read_energies(self) -> tuple[dict[str, int], dict[str, str], str]:
        energies: dict[str, int] = {}
        errors: dict[str, str] = {}

        if self._source_mode == "direct":
            energies, errors, permission_denied = self._read_direct_energies()
            if not permission_denied:
                return energies, errors, "direct"
            if self.cfg.use_sudo == "never":
                return {}, errors, "direct"
            self._source_mode = "helper"

        try:
            helper_energies = self._read_helper_energies()
        except RaplError as exc:
            self._helper_error = str(exc)
            return {}, {d.domain_id: str(exc) for d in self._domains.values()}, "helper"

        self._helper_error = None
        for domain in self._domains.values():
            if domain.domain_id in helper_energies:
                energies[domain.domain_id] = helper_energies[domain.domain_id]
            else:
                errors[domain.domain_id] = "energy_uj missing from RAPL helper output"
        return energies, errors, "helper"

    # ------------------------------------------------------------------ sampling

    def sample(self) -> tuple[RaplSample, ...]:
        self.refresh()
        if not self._domains:
            return ()

        energies, errors, mode = self._read_energies()
        now_mono = self._clock()
        timestamp = self._now()
        samples: list[RaplSample] = []

        for domain in self.all_domains:
            source = f"rapl:{domain.path}" if mode == "direct" else f"rapl-sudo:{domain.path}"
            energy = energies.get(domain.domain_id)
            error = errors.get(domain.domain_id)

            if not domain.enabled:
                samples.append(
                    RaplSample(domain.domain_id, domain.name, energy, None, None, timestamp, source, "RAPL domain disabled")
                )
                continue
            if energy is None:
                samples.append(
                    RaplSample(
                        domain.domain_id,
                        domain.name,
                        None,
                        self._energy_j.get(domain.domain_id),
                        None,
                        timestamp,
                        source,
                        error or "energy_uj unavailable",
                    )
                )
                continue

            baseline = self._baseline.get(domain.domain_id)
            if baseline is None:
                self._baseline[domain.domain_id] = (energy, now_mono)
                self._energy_j.setdefault(domain.domain_id, 0.0)
                self._last_power[domain.domain_id] = None
                samples.append(
                    RaplSample(
                        domain.domain_id,
                        domain.name,
                        energy,
                        0.0,
                        None,
                        timestamp,
                        source,
                        "warm-up: need a second sample",
                    )
                )
                continue

            previous_energy, previous_at = baseline
            dt = now_mono - previous_at
            if dt < self.cfg.min_interval_s:
                samples.append(
                    RaplSample(
                        domain.domain_id,
                        domain.name,
                        energy,
                        self._energy_j.get(domain.domain_id),
                        self._last_power.get(domain.domain_id),
                        timestamp,
                        source,
                        None,
                    )
                )
                continue
            if dt <= 0:
                samples.append(
                    RaplSample(
                        domain.domain_id,
                        domain.name,
                        energy,
                        self._energy_j.get(domain.domain_id),
                        None,
                        timestamp,
                        source,
                        "non-monotonic clock",
                    )
                )
                continue

            if energy >= previous_energy:
                delta_uj = energy - previous_energy
            elif domain.max_energy_uj > 0 and domain.max_energy_uj >= previous_energy:
                delta_uj = energy + domain.max_energy_uj - previous_energy
            else:
                self._baseline[domain.domain_id] = (energy, now_mono)
                self._last_power[domain.domain_id] = None
                samples.append(
                    RaplSample(
                        domain.domain_id,
                        domain.name,
                        energy,
                        self._energy_j.get(domain.domain_id),
                        None,
                        timestamp,
                        source,
                        "energy counter reset or wrap without max_energy_range_uj",
                    )
                )
                continue

            power_w = (delta_uj / 1_000_000.0) / dt
            if power_w > self.cfg.max_plausible_w:
                self._baseline[domain.domain_id] = (energy, now_mono)
                self._last_power[domain.domain_id] = None
                samples.append(
                    RaplSample(
                        domain.domain_id,
                        domain.name,
                        energy,
                        self._energy_j.get(domain.domain_id),
                        None,
                        timestamp,
                        source,
                        f"implausible power {power_w:.1f} W (counter reset?); baseline reset",
                    )
                )
                continue

            self._baseline[domain.domain_id] = (energy, now_mono)
            self._energy_j[domain.domain_id] = self._energy_j.get(domain.domain_id, 0.0) + delta_uj / 1_000_000.0
            self._last_power[domain.domain_id] = power_w
            samples.append(
                RaplSample(
                    domain.domain_id,
                    domain.name,
                    energy,
                    self._energy_j[domain.domain_id],
                    power_w,
                    timestamp,
                    source,
                    None,
                )
            )

        return tuple(samples)
