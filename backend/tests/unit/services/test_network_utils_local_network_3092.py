"""`find_local_ipv4_network` reads the prefix off the interface that owns the
address (upstream 3a5f802c, #3092): an IPv4 address carries no prefix, and the
subnet check assumed /24 for both sides — splitting a /22 LAN and merging a /25.
"""

from collections import namedtuple
from unittest.mock import patch

from backend.app.services import network_utils

_IP_ADDR_JSON = """[
  {"ifname": "lo", "addr_info": [{"family": "inet", "local": "127.0.0.1", "prefixlen": 8}]},
  {"ifname": "enp3s0", "addr_info": [{"family": "inet", "local": "192.168.96.9", "prefixlen": 22}]},
  {"ifname": "enp4s0", "addr_info": [
     {"family": "inet", "local": "10.0.0.5", "prefixlen": 24},
     {"family": "inet", "local": "10.0.0.6", "prefixlen": 24, "label": "enp4s0:vp1"}
  ]},
  {"ifname": "docker0", "addr_info": [{"family": "inet", "local": "172.17.0.1", "prefixlen": 16}]}
]"""


def _fake_ip_addr():
    """Patch `ip -j addr show` with a fixed multi-homed Linux host."""
    result = namedtuple("CompletedProcess", ["returncode", "stdout", "stderr"])(0, _IP_ADDR_JSON, "")
    return patch.object(network_utils, "subprocess", **{"run.return_value": result})


class TestFindLocalIPv4Network:
    """#3092: an address carries no prefix, so it has to be read off the interface."""

    def test_reads_the_configured_prefix_not_a_guessed_24(self):
        with _fake_ip_addr(), patch.object(network_utils, "_IP_CMD", "/usr/sbin/ip"):
            assert str(network_utils.find_local_ipv4_network("192.168.96.9")) == "192.168.96.0/22"

    def test_an_alias_address_resolves_too(self):
        # The VP binds aliases; an alias is a perfectly good route source.
        with _fake_ip_addr(), patch.object(network_utils, "_IP_CMD", "/usr/sbin/ip"):
            assert str(network_utils.find_local_ipv4_network("10.0.0.6")) == "10.0.0.0/24"

    def test_an_excluded_interface_still_answers(self):
        """EXCLUDED_INTERFACE_PREFIXES keeps docker0 out of the VP dropdown.

        It must not also make the kernel's own choice of route source
        unanswerable — "unknown" would be a worse answer than the truth.
        """
        with _fake_ip_addr(), patch.object(network_utils, "_IP_CMD", "/usr/sbin/ip"):
            assert str(network_utils.find_local_ipv4_network("172.17.0.1")) == "172.17.0.0/16"
            assert not [i for i in network_utils.get_all_interface_ips() if i["name"] == "docker0"]

    def test_an_address_no_interface_holds_is_none(self):
        with _fake_ip_addr(), patch.object(network_utils, "_IP_CMD", "/usr/sbin/ip"):
            assert network_utils.find_local_ipv4_network("192.168.1.1") is None

    def test_a_hostname_is_none(self):
        assert network_utils.find_local_ipv4_network("printer.local") is None
