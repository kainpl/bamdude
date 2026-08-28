"""The virtual printer's ports are below 1024, and that has a permission cost.

Ported from upstream `28a6ca6f` (#2549), where a reporter spent several days on
Discord over one missing line in a unit file. The failure is invisible from the
outside: the sockets never open, the slicer never finds the printer, and the
port checks report the same "nothing is listening" as an ordinary port conflict.
The only trace is one EACCES line in the journal.

⚠️ **Adapted, not copied.** Upstream keys the check on FTPS 990 because that is
the port their server mode binds. Ours also runs a **proxy** mode whose ports
come from the proxy manager at runtime and need not be privileged at all — so
the check reads the ports actually probed and fires on any of them below 1024,
rather than assuming a number.
"""

from __future__ import annotations

from unittest.mock import mock_open, patch

from backend.app.services.virtual_printer.diagnostic import (
    _CAP_NET_BIND_SERVICE,
    _PRIVILEGED_PORT_CEILING,
    can_bind_privileged_ports,
)


def _capeff(bits: int) -> str:
    return f"Name:\tpython3\nCapEff:\t{bits:016x}\nCapBnd:\t0000000000000000\n"


class TestReadingTheCapability:
    def test_root_may_always_bind(self):
        with patch("os.geteuid", create=True, return_value=0):
            assert can_bind_privileged_ports() is True

    def test_the_capability_is_read_from_the_effective_set(self):
        granted = _capeff(1 << _CAP_NET_BIND_SERVICE)
        with patch("os.geteuid", create=True, return_value=1000), patch("builtins.open", mock_open(read_data=granted)):
            assert can_bind_privileged_ports() is True

    def test_a_process_without_it_says_so(self):
        with (
            patch("os.geteuid", create=True, return_value=1000),
            patch("builtins.open", mock_open(read_data=_capeff(0))),
        ):
            assert can_bind_privileged_ports() is False

    def test_other_capabilities_do_not_count_as_this_one(self):
        """A neighbouring bit must not read as permission — the shift is the test."""
        neighbour = _capeff(1 << (_CAP_NET_BIND_SERVICE + 1))
        with (
            patch("os.geteuid", create=True, return_value=1000),
            patch("builtins.open", mock_open(read_data=neighbour)),
        ):
            assert can_bind_privileged_ports() is False

    def test_no_procfs_is_unknown_rather_than_false(self):
        """macOS and Windows have no capability model at all. Answering False
        there would put a permanent red row on a working install."""
        with patch("os.geteuid", create=True, return_value=1000), patch("builtins.open", side_effect=OSError):
            assert can_bind_privileged_ports() is None

    def test_an_unreadable_capability_line_is_unknown_too(self):
        garbage = "Name:\tpython3\nCapEff:\tnot-a-number\n"
        with patch("os.geteuid", create=True, return_value=1000), patch("builtins.open", mock_open(read_data=garbage)):
            assert can_bind_privileged_ports() is None

    def test_a_status_file_without_the_line_is_unknown(self):
        with (
            patch("os.geteuid", create=True, return_value=1000),
            patch("builtins.open", mock_open(read_data="Name:\tpython3\n")),
        ):
            assert can_bind_privileged_ports() is None

    def test_a_platform_without_geteuid_still_answers(self):
        """``os.geteuid`` does not exist on Windows at all — which is why the
        helper reaches for it with ``getattr`` rather than calling it. Written
        after the first draft of these tests failed on exactly that: patching
        ``os.geteuid`` needs ``create=True`` here, because the attribute is not
        there to patch."""
        import types

        no_geteuid = types.SimpleNamespace()
        with (
            patch("backend.app.services.virtual_printer.diagnostic.os", no_geteuid),
            patch("builtins.open", side_effect=OSError),
        ):
            assert can_bind_privileged_ports() is None


class TestWhichPortsCount:
    def test_the_ceiling_is_the_posix_one(self):
        assert _PRIVILEGED_PORT_CEILING == 1024

    def test_the_vp_server_port_is_below_it(self):
        from backend.app.services.virtual_printer.diagnostic import PORT_FTPS

        assert PORT_FTPS < _PRIVILEGED_PORT_CEILING

    def test_the_other_server_ports_are_not(self):
        """Only FTPS is privileged in server mode, so a missing capability
        cannot be blamed for a dead MQTT or bind listener."""
        from backend.app.services.virtual_printer.diagnostic import (
            PORT_BIND,
            PORT_BIND_PLAIN,
            PORT_MQTT,
        )

        for port in (PORT_MQTT, PORT_BIND, PORT_BIND_PLAIN):
            assert port >= _PRIVILEGED_PORT_CEILING
