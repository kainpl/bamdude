"""Reading the OIDC provider out of ``BAMDUDE_OIDC_*`` (upstream #2593).

The reader is deliberately dumb — it reads, strips and defaults, and hands the
result to the same ``OIDCProviderCreate`` schema the API uses, so env config
cannot reach a state the UI would have refused.

Two rules carry the weight and both were learned the hard way upstream: an empty
required variable is *unset*, not an intentional empty value; and every value is
stripped, because a Kubernetes Secret written as a block scalar carries a
trailing newline that nothing downstream rejects.
"""

from __future__ import annotations

import pytest

from backend.app.core.oidc_env import EnvOIDCConfigError, env_bool, parse_bool, read_env_oidc_config

_REQUIRED = {
    "BAMDUDE_OIDC_NAME": "Authentik",
    "BAMDUDE_OIDC_ISSUER_URL": "https://id.example.com",
    "BAMDUDE_OIDC_CLIENT_ID": "bamdude",
    "BAMDUDE_OIDC_CLIENT_SECRET": "s3cr3t",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    """No BAMDUDE_OIDC_* leaks in from the developer's own shell."""
    for key in list(__import__("os").environ):
        if key.startswith("BAMDUDE_OIDC_"):
            monkeypatch.delenv(key, raising=False)


def _set(monkeypatch, **overrides):
    for k, v in {**_REQUIRED, **overrides}.items():
        if v is None:
            monkeypatch.delenv(k, raising=False)
        else:
            monkeypatch.setenv(k, v)


class TestRequiredSet:
    def test_nothing_configured_is_none_not_an_error(self):
        # The overwhelmingly common case: an install that never wanted this.
        assert read_env_oidc_config() is None

    @pytest.mark.parametrize("missing", sorted(_REQUIRED))
    def test_any_one_missing_applies_nothing(self, monkeypatch, missing):
        """All four or none.

        A provider written without its secret would be stored and enabled, and
        then fail at authorize time — long after the operator could connect that
        to a typo in their compose file.
        """
        _set(monkeypatch, **{missing: None})
        assert read_env_oidc_config() is None

    @pytest.mark.parametrize("blank", ["", "   ", "\n"])
    def test_a_blank_required_value_counts_as_unset(self, monkeypatch, blank):
        # `BAMDUDE_OIDC_CLIENT_SECRET=` in a compose file is a forgotten value,
        # not an intentional empty secret.
        _set(monkeypatch, BAMDUDE_OIDC_CLIENT_SECRET=blank)
        assert read_env_oidc_config() is None


class TestStripping:
    def test_every_required_value_is_stripped(self, monkeypatch):
        """The Kubernetes block-scalar case.

        ``stringData: secret: |`` and file-backed secrets both carry a trailing
        newline, and ``max_length`` is the only bound the schema puts on these
        four. An issuer_url with a newline is stored, enabled, and then dies with
        httpx.InvalidURL on the first click of the SSO button.
        """
        _set(
            monkeypatch,
            BAMDUDE_OIDC_NAME="  Authentik\n",
            BAMDUDE_OIDC_ISSUER_URL="https://id.example.com\n",
            BAMDUDE_OIDC_CLIENT_ID=" bamdude ",
            BAMDUDE_OIDC_CLIENT_SECRET="s3cr3t\n",
        )
        cfg = read_env_oidc_config()
        assert cfg["name"] == "Authentik"
        assert cfg["issuer_url"] == "https://id.example.com"
        assert cfg["client_id"] == "bamdude"
        assert cfg["client_secret"] == "s3cr3t"


class TestOptionalDefaults:
    def test_the_defaults_match_the_schema(self, monkeypatch):
        _set(monkeypatch)
        cfg = read_env_oidc_config()
        assert cfg["scopes"] == "openid email profile"
        assert cfg["is_enabled"] is True
        assert cfg["auto_create_users"] is False
        assert cfg["auto_link_existing_accounts"] is False
        assert cfg["email_claim"] == "email"
        assert cfg["require_email_verified"] is True
        assert cfg["icon_url"] is None
        assert cfg["is_autologin"] is False
        assert cfg["default_group"] is None

    @pytest.mark.parametrize("blank", ["", "   "])
    def test_a_blank_optional_falls_back_rather_than_refusing(self, monkeypatch, blank):
        # An empty optional in a compose file means "leave it alone". Refusing
        # the whole config over one would be a startup failure for a non-value.
        _set(monkeypatch, BAMDUDE_OIDC_SCOPES=blank, BAMDUDE_OIDC_EMAIL_CLAIM=blank, BAMDUDE_OIDC_ENABLED=blank)
        cfg = read_env_oidc_config()
        assert cfg["scopes"] == "openid email profile"
        assert cfg["email_claim"] == "email"
        assert cfg["is_enabled"] is True

    def test_the_default_group_is_read_as_a_name(self, monkeypatch):
        # Not an id: ids are assigned per install, so the same compose file would
        # point at a different group on every deployment. Resolution against the
        # DB happens in the applier — the reader has no session.
        _set(monkeypatch, BAMDUDE_OIDC_DEFAULT_GROUP=" Operators ")
        assert read_env_oidc_config()["default_group"] == "Operators"


class TestBooleans:
    @pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on", " True "])
    def test_truthy_vocabulary(self, monkeypatch, value):
        monkeypatch.setenv("X", value)
        assert env_bool("X", False) is True

    @pytest.mark.parametrize("value", ["false", "FALSE", "0", "no", "off", " False "])
    def test_falsy_vocabulary(self, monkeypatch, value):
        monkeypatch.setenv("X", value)
        assert env_bool("X", True) is False

    @pytest.mark.parametrize("value", ["", "   "])
    def test_blank_is_unset(self, monkeypatch, value):
        monkeypatch.setenv("X", value)
        assert env_bool("X", True) is True
        assert env_bool("X", False) is False

    def test_an_unrecognised_value_is_refused_loudly_by_default(self, monkeypatch):
        """A typo must not be read as the wrong thing.

        ``BAMDUDE_OIDC_ENABLED=ture`` silently meaning "false" would leave the
        operator staring at a login page with no SSO button and nothing to go on.
        """
        monkeypatch.setenv("X", "ture")
        with pytest.raises(EnvOIDCConfigError) as exc:
            env_bool("X", True)
        # The message names the variable and its value — booleans are not secret.
        assert "X" in str(exc.value)
        assert "ture" in str(exc.value)

    def test_lenient_mode_falls_back_instead_of_raising(self, monkeypatch):
        """For a caller on a request path.

        The local-login bypass is the recovery route for an install nobody can
        log into; a typo there must not turn the login endpoint into a 500.
        """
        monkeypatch.setenv("X", "ture")
        assert env_bool("X", False, strict=False) is False
        assert env_bool("X", True, strict=False) is True

    def test_parse_bool_shares_the_vocabulary(self):
        # One definition of the accepted words. Two independent copies existed
        # before this and had already drifted — one accepted "on", one did not.
        assert parse_bool("on", False) is True
        assert parse_bool("off", True) is False
        assert parse_bool("nonsense", True) is True
        assert parse_bool(None, True) is True
