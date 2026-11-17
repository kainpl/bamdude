"""Two things a connection diagnostic could not say.

* Which step hung: a check that overran its 15 s budget left ``timed_out`` and
  nothing else, for every printer of a farm at once (upstream #3164).
* That a macvlan / ipvlan container is on the LAN: its interface looks like a
  veth to sysfs, so it was told it runs on bridge networking and should be
  recreated with host networking (upstream #3417).
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from backend.app.services import diagnostic_snapshot, discovery, printer_diagnostic

MOD = "backend.app.services.printer_diagnostic"


def _ip_links(*links):
    def run(cmd, **kwargs):
        assert cmd[:2] == ["ip", "-d"]
        return SimpleNamespace(returncode=0, stdout=json.dumps(list(links)))

    return run


def _link(name, kind=None, mode=None):
    link = {"ifname": name}
    if kind:
        link["linkinfo"] = {"info_kind": kind, "info_data": {"mode": mode} if mode else {}}
    return link


@pytest.mark.parametrize(
    ("links", "expected"),
    [
        ([_link("eth0", "veth")], "none"),
        ([_link("eth0", "macvlan", "bridge")], "macvlan"),
        ([_link("eth0", "ipvlan", "l2")], "ipvlan"),
        ([_link("eth0", "ipvlan", "l3")], "ipvlan_l3"),
        ([_link("eth0", "ipvlan", "l3s")], "ipvlan_l3"),
        # Attached to a bridge network AND a macvlan: the LAN is reached through the macvlan.
        ([_link("eth0", "veth"), _link("eth1", "macvlan", "bridge")], "macvlan"),
    ],
)
def test_the_link_kind_is_read_from_the_kernel(monkeypatch, links, expected):
    monkeypatch.setattr(f"{MOD}.subprocess.run", _ip_links(*links))

    assert printer_diagnostic._lan_link_mode() == expected


def test_link_kinds_that_cannot_be_read_are_unknown(monkeypatch):
    def run(cmd, **kwargs):
        raise FileNotFoundError("ip")

    monkeypatch.setattr(f"{MOD}.subprocess.run", run)

    assert printer_diagnostic._lan_link_mode() is None


@pytest.mark.parametrize(
    ("lan", "mode"),
    [("none", "bridge"), ("macvlan", "macvlan"), ("ipvlan", "ipvlan"), ("ipvlan_l3", "ipvlan_l3"), (None, None)],
)
def test_an_isolated_container_is_bridge_only_when_its_link_says_so(monkeypatch, lan, mode):
    monkeypatch.setattr(f"{MOD}.socket.if_nameindex", lambda: [(1, "lo"), (2, "eth0")])
    monkeypatch.setattr(f"{MOD}._has_native_interface", lambda: False)
    monkeypatch.setattr(f"{MOD}._lan_link_mode", lambda: lan)

    assert printer_diagnostic._detect_container_network_mode(discovery.RUNTIME_DOCKER) == mode


@pytest.mark.asyncio
async def test_a_timed_out_check_names_its_step_and_keeps_what_finished(monkeypatch):
    finished = SimpleNamespace(model_dump=lambda: {"id": "port_mqtt", "status": "pass"})

    async def hangs(ip_address, *, progress=None, **kwargs):
        progress["checks"] = [finished]
        progress["stage"] = "mqtt_auth"
        await asyncio.sleep(30)

    monkeypatch.setattr(f"{MOD}.run_connection_diagnostic", hangs)
    monkeypatch.setattr(diagnostic_snapshot, "_PER_DIAGNOSTIC_TIMEOUT_SECONDS", 0.05)
    printer = SimpleNamespace(id=3, name="P", ip_address="192.0.2.9", serial_number="S", access_code="c")

    entry = await diagnostic_snapshot._run_connection_for(printer)

    assert entry["error"] == "timed_out"
    assert entry["stalled_in"] == "mqtt_auth"
    assert entry["checks"] == [{"id": "port_mqtt", "status": "pass"}]
    assert entry["elapsed_s"] >= 0


def test_every_step_of_the_diagnostic_names_itself():
    import inspect

    source = inspect.getsource(printer_diagnostic.run_connection_diagnostic)
    stages = [
        "ports",
        "macos_local_network",
        "network_mode",
        "subnet",
        "mqtt_auth",
        "developer_mode",
        "external_storage",
        "printer_publishing",
    ]
    positions = [source.index(f'progress["stage"] = "{stage}"') for stage in stages]
    assert positions == sorted(positions)
