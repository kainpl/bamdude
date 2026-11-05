import pytest
from orca_bamdude.config import ConfigError, Connection, canonical_base_url, validate_extra_headers


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://farm.example", "https://farm.example"),
        ("HTTPS://Farm.Example/", "https://farm.example"),
        ("http://192.168.1.5:8000", "http://192.168.1.5:8000"),
        ("https://farm.example/bamdude/", "https://farm.example/bamdude"),
        ("  https://farm.example:8443/a/b  ", "https://farm.example:8443/a/b"),
        ("http://[::1]:8000", "http://[::1]:8000"),
    ],
)
def test_a_server_url_is_canonical(raw, expected):
    assert canonical_base_url(raw) == expected


@pytest.mark.parametrize(
    "raw,code",
    [
        ("ftp://farm.example", "url_scheme"),
        ("farm.example", "url_scheme"),
        ("https://user:pass@farm.example", "url_userinfo"),
        ("https://farm.example/?a=1", "url_query"),
        ("https://farm.example/#x", "url_query"),
        ("https://farm.example?", "url_query"),
        ("https://", "url_host"),
        ("https://farm.example:notaport", "url_invalid"),
        ("https://farm example", "url_invalid"),
    ],
)
def test_a_server_url_outside_the_rules_is_refused(raw, code):
    with pytest.raises(ConfigError) as refused:
        canonical_base_url(raw)
    assert refused.value.code == code


def test_proxy_headers_are_checked_and_kept():
    pairs = validate_extra_headers([("CF-Access-Client-Id", "id"), ("CF-Access-Client-Secret", "secret")])
    assert pairs == (("CF-Access-Client-Id", "id"), ("CF-Access-Client-Secret", "secret"))


@pytest.mark.parametrize(
    "pairs,code",
    [
        ([("X-API-Key", "x")], "header_reserved"),
        ([("Authorization", "x")], "header_reserved"),
        ([("host", "x")], "header_reserved"),
        ([("CF-Connecting-IP", "x")], "header_reserved"),
        ([("A", "1"), ("a", "2")], "header_reserved"),
        ([("Bad Name", "x")], "header_name"),
        ([("X-Ok", "line\r\nInjected: 1")], "header_value"),
        ([("X-Ok", "x" * 2000)], "header_value"),
        ([(f"X-{n}", "v") for n in range(9)], "header_count"),
    ],
)
def test_proxy_headers_outside_the_rules_are_refused(pairs, code):
    with pytest.raises(ConfigError) as refused:
        validate_extra_headers(pairs)
    assert refused.value.code == code


def test_a_connection_never_shows_its_key(tmp_path):
    connection = Connection.build("https://farm.example", "bd_secret_value")
    assert "bd_secret_value" not in repr(connection)
    with pytest.raises(ConfigError) as refused:
        Connection.build("https://farm.example", "bd_x", ca_file=str(tmp_path / "missing.pem"))
    assert refused.value.code == "ca_file"
    with pytest.raises(ConfigError):
        Connection.build("https://farm.example", "has space")
