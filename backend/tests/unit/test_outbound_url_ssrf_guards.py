"""Every outbound-URL setting is guarded, and no new one slips in unguarded.

BamDude makes requests to addresses the operator types in: Home Assistant, the
Obico ML endpoint, two slicer sidecars, Spoolman. Left unchecked, those are an
SSRF surface — the classic target being a cloud-provider metadata endpoint.

There are exactly **two** policies, and which applies is a property of the
service, not the caller:

* **LAN-service** — loopback and RFC-1918 are *permitted*, because self-hosting
  these next to BamDude is the normal topology, not an attack. What is rejected
  is dangerous anywhere: non-HTTP schemes, numeric-encoded IPs, cloud metadata,
  multicast, unspecified, and IPv4-mapped IPv6 encodings of those.
* **Public-internet** — for the OIDC issuer and icons, where a private address
  is a probe rather than a configuration.

The last class is the one that earns its keep: it imports the real settings
model and fails when an outbound-URL field is added without a policy.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes._spoolman_helpers import assert_safe_spoolman_url
from backend.app.api.routes._url_safety import assert_safe_lan_service_url
from backend.app.schemas.auth import _validate_issuer_url
from backend.app.schemas.settings import LAN_SERVICE_URL_SETTINGS, AppSettings, AppSettingsUpdate

DANGEROUS = [
    ("file:///etc/passwd", "non-HTTP scheme"),
    ("gopher://host/x", "non-HTTP scheme"),
    ("http://169.254.169.254/latest/meta-data/", "AWS/GCP/Azure metadata"),
    ("http://100.100.100.200/", "Alibaba metadata"),
    ("http://2130706433/", "decimal-encoded 127.0.0.1"),
    ("http://0x7f000001/", "hex-encoded 127.0.0.1"),
    ("http://[::ffff:169.254.169.254]/", "IPv4-mapped metadata"),
    ("http://224.0.0.1/", "multicast"),
    ("http://0.0.0.0/", "unspecified"),
]

# The topology the LAN policy exists to keep working.
LEGITIMATE_LAN = [
    "http://192.168.1.100:8123",
    "http://10.0.0.5:7912",
    "http://172.16.4.4:3333",
    "http://127.0.0.1:8080",
    "http://localhost:5000",
    "https://spoolman.lan",
]


class TestLanServicePolicy:
    @pytest.mark.parametrize(("url", "why"), DANGEROUS)
    def test_rejects(self, url: str, why: str) -> None:
        with pytest.raises(ValueError):
            assert_safe_lan_service_url(url, label="test URL")

    @pytest.mark.parametrize("url", LEGITIMATE_LAN)
    def test_permits_self_hosted_neighbours(self, url: str) -> None:
        assert_safe_lan_service_url(url, label="test URL")

    def test_the_message_names_the_field(self) -> None:
        """The label is the whole reason this takes one — an operator needs to
        know which box to fix."""
        with pytest.raises(ValueError, match="ntfy server URL"):
            assert_safe_lan_service_url("file:///x", label="ntfy server URL")

    def test_a_symbolic_hostname_is_not_resolved(self) -> None:
        """Resolving here would be a TOCTOU (DNS can move between validation and
        request) and a network call a validator has no business making."""
        assert_safe_lan_service_url("http://this-host-does-not-exist.invalid/", label="test URL")

    def test_spoolman_is_the_same_policy_under_its_own_name(self) -> None:
        """It was a full copy of this logic, with its own metadata set and its
        own inline regex. Now an alias — pinned so it cannot fork again."""
        with pytest.raises(ValueError, match="Spoolman URL"):
            assert_safe_spoolman_url("http://169.254.169.254/")
        assert_safe_spoolman_url("http://192.168.1.5:7912")


class TestPublicInternetPolicy:
    def test_rejects_what_the_hand_rolled_check_used_to_allow(self) -> None:
        """Measured against the old validator, these two got through: it tested
        only ``is_private | is_loopback | is_link_local``, and Python's
        ``ipaddress`` raises on a numeric-encoded host rather than classifying
        it, so the except-branch let it pass."""
        for url in ("https://2130706433/", "https://224.0.0.1/"):
            with pytest.raises(ValueError):
                _validate_issuer_url(url)

    def test_still_rejects_private_and_loopback(self) -> None:
        for url in ("https://127.0.0.1/", "https://10.0.0.5/", "https://[::ffff:127.0.0.1]/"):
            with pytest.raises(ValueError):
                _validate_issuer_url(url)

    def test_still_requires_https(self) -> None:
        with pytest.raises(ValueError, match="https"):
            _validate_issuer_url("http://sso.example.com/")

    def test_accepts_a_real_issuer(self) -> None:
        assert _validate_issuer_url("https://sso.example.com/realms/main") is not None

    def test_the_message_names_the_field_not_the_icon(self) -> None:
        """The guard is shared with the icon-URL check; its wording is rewritten
        so the user is not told to fix a field they never touched."""
        with pytest.raises(ValueError, match="issuer_url"):
            _validate_issuer_url("https://127.0.0.1/")


class TestSettingsAreValidatedOnSave:
    @pytest.mark.parametrize("field", LAN_SERVICE_URL_SETTINGS)
    def test_each_guarded_setting_rejects_a_metadata_endpoint(self, field: str) -> None:
        with pytest.raises(ValueError, match=field):
            AppSettingsUpdate(**{field: "http://169.254.169.254/"})

    @pytest.mark.parametrize("field", LAN_SERVICE_URL_SETTINGS)
    def test_each_guarded_setting_accepts_a_lan_address(self, field: str) -> None:
        AppSettingsUpdate(**{field: "http://192.168.1.50:8080"})

    @pytest.mark.parametrize("field", LAN_SERVICE_URL_SETTINGS)
    def test_empty_means_not_configured(self, field: str) -> None:
        AppSettingsUpdate(**{field: ""})


class TestNoNewOutboundUrlEscapesAPolicy:
    """The backstop. Everything above tests today's fields; this one fails when
    tomorrow's is added without a decision.
    """

    # Deliberately exempt, each for a stated reason. Adding to this set is a
    # decision someone has to write down — which is the point.
    EXEMPT = {
        # Our own externally-visible address, not a destination we fetch.
        "external_url",
        # Not http(s) at all — ldap:// / ldaps://, with its own connection path.
        "ldap_server_url",
        # Guarded by assert_safe_spoolman_url at its own call sites, which is the
        # same LAN policy under a Spoolman-specific label.
        "spoolman_url",
    }

    def test_every_url_setting_is_guarded_or_explicitly_exempt(self) -> None:
        url_fields = {name for name in AppSettings.model_fields if name.endswith("_url")}
        unaccounted = url_fields - set(LAN_SERVICE_URL_SETTINGS) - self.EXEMPT
        assert unaccounted == set(), (
            f"Outbound URL setting(s) with no SSRF policy: {sorted(unaccounted)}. "
            "Add to LAN_SERVICE_URL_SETTINGS, or to EXEMPT here with the reason."
        )

    def test_the_exempt_set_has_not_gone_stale(self) -> None:
        """An exemption for a field that no longer exists is a comment pretending
        to be a guarantee."""
        url_fields = {name for name in AppSettings.model_fields if name.endswith("_url")}
        assert url_fields >= self.EXEMPT, f"EXEMPT names a setting that no longer exists: {self.EXEMPT - url_fields}"

    def test_every_guarded_name_is_a_real_setting(self) -> None:
        missing = set(LAN_SERVICE_URL_SETTINGS) - set(AppSettings.model_fields)
        assert missing == set(), f"LAN_SERVICE_URL_SETTINGS names a setting that does not exist: {sorted(missing)}"
