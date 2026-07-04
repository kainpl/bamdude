"""Windows psutil interface-enumeration path for network_utils.

The Linux path uses fcntl ioctls (unavailable on Windows), so
``get_network_interfaces`` routes to ``_get_network_interfaces_psutil`` on
win32. These tests exercise that helper directly (mocking psutil) so the
filtering logic is covered on any platform.
"""

import socket
from types import SimpleNamespace
from unittest.mock import patch

from backend.app.services import network_utils


def _addr(ip, netmask, family=socket.AF_INET):
    return SimpleNamespace(family=family, address=ip, netmask=netmask, broadcast=None, ptp=None)


def _stats(isup=True):
    return SimpleNamespace(isup=isup, duplex=0, speed=0, mtu=1500)


def test_psutil_path_returns_primary_ipv4_with_subnet():
    addrs = {"eth0": [_addr("192.168.1.50", "255.255.255.0")]}
    stats = {"eth0": _stats(isup=True)}
    with (
        patch("psutil.net_if_addrs", return_value=addrs),
        patch("psutil.net_if_stats", return_value=stats),
    ):
        result = network_utils._get_network_interfaces_psutil()
    assert result == [{"name": "eth0", "ip": "192.168.1.50", "netmask": "255.255.255.0", "subnet": "192.168.1.0/24"}]


def test_psutil_path_skips_loopback_linklocal_and_down():
    addrs = {
        "lo": [_addr("127.0.0.1", "255.0.0.0")],
        "linklocal": [_addr("169.254.10.20", "255.255.0.0")],
        "down_if": [_addr("10.0.0.5", "255.255.255.0")],
        "wlan0": [_addr("10.1.1.7", "255.255.255.0")],
    }
    stats = {
        "lo": _stats(isup=True),
        "linklocal": _stats(isup=True),
        "down_if": _stats(isup=False),
        "wlan0": _stats(isup=True),
    }
    with (
        patch("psutil.net_if_addrs", return_value=addrs),
        patch("psutil.net_if_stats", return_value=stats),
    ):
        result = network_utils._get_network_interfaces_psutil()
    names = {r["name"] for r in result}
    assert names == {"wlan0"}


def test_psutil_path_skips_non_ipv4_and_takes_first():
    addrs = {
        "eth0": [
            _addr("fe80::1", None, family=socket.AF_INET6),
            _addr("192.168.5.10", "255.255.255.0"),
            _addr("192.168.5.11", "255.255.255.0"),
        ]
    }
    stats = {"eth0": _stats(isup=True)}
    with (
        patch("psutil.net_if_addrs", return_value=addrs),
        patch("psutil.net_if_stats", return_value=stats),
    ):
        result = network_utils._get_network_interfaces_psutil()
    assert [r["ip"] for r in result] == ["192.168.5.10"]  # first IPv4 only


def test_get_network_interfaces_routes_to_psutil_on_win32():
    with (
        patch.object(network_utils.sys, "platform", "win32"),
        patch.object(network_utils, "_get_network_interfaces_psutil", return_value=[{"name": "x"}]) as mock,
    ):
        result = network_utils.get_network_interfaces()
    mock.assert_called_once()
    assert result == [{"name": "x"}]
