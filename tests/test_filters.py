from __future__ import annotations

from app.collectors.disk import _parent_device
from app.collectors.network import filter_interfaces
from app.config import NetworkConfig


def test_default_network_filter_hides_virtual_interfaces() -> None:
    cfg = NetworkConfig()
    names = ["lo", "lan0", "lan1", "docker0", "br-1234", "veth99", "virbr0", "tun0"]
    assert filter_interfaces(names, cfg) == ["lan0", "lan1"]


def test_include_list_overrides_exclude() -> None:
    cfg = NetworkConfig(include_interfaces=["docker*", "lan0"])
    names = ["lo", "lan0", "docker0", "br-1"]
    assert filter_interfaces(names, cfg) == ["docker0", "lan0"]


def test_parent_device_mapping() -> None:
    known = {"nvme0n1", "sda", "dm-0"}
    assert _parent_device("nvme0n1p2", known) == "nvme0n1"
    assert _parent_device("sda1", known) == "sda"
    assert _parent_device("dm-0", known) == "dm-0"
    assert _parent_device("zram0", known) is None
