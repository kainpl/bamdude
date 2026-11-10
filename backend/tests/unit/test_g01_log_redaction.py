"""AC32/33: formatted/live and historical output share credential rules."""

import base64
import io
import json
import logging
import sys
from urllib.parse import quote

import pytest
from uvicorn.logging import AccessFormatter

from backend.app.core.logging_filters import CredentialSafeFormatter, redact_log_credentials, redact_url_credentials
from backend.app.services import log_reader
from backend.app.services.diagnostic_snapshot import _sanitize_recursive
from backend.app.utils.ffmpeg_output import summarize_ffmpeg_stderr

SHAPES = [
    "https://CANARY_BARE@host/path",
    "https://:CANARY_EMPTY_USER@host/path",
    "https://user:CANARY@tail@host/path",
    "https://host/path?access_token=CANARY_QUERY&limit=2",
    "https://host/path?api-key=CANARY_QUERY",
    "https://farm/reset#reset_token=CANARY_FRAGMENT",
    "Authorization: Basic CANARY_BASIC",
    '"Authorization": "Bearer CANARY_JSON"',
    "Proxy-Authorization: Digest CANARY_DIGEST",
    "Illegal header value b'CANARY_REFUSED\\n'",
    "password='CANARY_UNICODE_пароль with spaces'",
    "https://user:CANARY malformed@host/path",
    "https://hooks.slack.com/services/T/B/CANARY_PATH",
    "https://discord.com/api/webhooks/1/CANARY_PATH",
    "https://open.feishu.cn/open-apis/bot/v2/hook/CANARY_PATH",
    "https://api.day.app/CANARY_DEVICE/message",
    "ValidationError input_value='CANARY_VALIDATION', input_type=str",
    "[parameters: ('CANARY_SQL',)]\n(Background on this error at: https://sqlalche.me/e/20)",
    "password='first\nCANARY_MULTILINE_SECRET'",
    '"client_secret": "first\r\nCANARY_MULTILINE_JSON"',
    "Authorization: Bearer first\n CANARY_FOLDED_HEADER",
    "Cookie: first\r\n\tCANARY_FOLDED_COOKIE",
    '"Authorization":\n "Bearer first\nCANARY_QUOTED_HEADER"',
    "Illegal header value b'first\nCANARY_MULTILINE_REFUSED'",
]


@pytest.mark.parametrize("source", SHAPES)
@pytest.mark.parametrize("encoding", ["raw", "percent", "double-percent", "json-slashes", "unicode-escapes", "json"])
def test_live_and_historical_shapes(source, encoding):
    if encoding == "percent":
        source = quote(source, safe="")
    elif encoding == "double-percent":
        source = quote(quote(source, safe=""), safe="")
    elif encoding == "json-slashes":
        source = source.replace("/", r"\/")
    elif encoding == "unicode-escapes":
        source = "".join(f"\\u{ord(char):04x}" for char in source)
    elif encoding == "json":
        source = json.dumps(source)[1:-1]
    for redact in (redact_log_credentials, log_reader.sanitize_log_content):
        assert "CANARY" not in redact(source)


def test_formatted_args_traceback_cached_exception_and_two_handlers():
    try:
        raise ValueError("invalid header value b'CANARY_EXCEPTION'")
    except ValueError:
        record = logging.LogRecord(
            "uvicorn.error", logging.ERROR, __file__, 1, "request %s", ("https://h/?token=CANARY_ARG",), sys.exc_info()
        )
    original_args = record.args
    first = CredentialSafeFormatter("%(message)s").format(record)
    assert "CANARY" not in first and "ValueError" in first
    assert record.args is original_args and record.exc_text is None
    record.exc_text = "Authorization: Basic CANARY_CACHED"
    second = CredentialSafeFormatter("%(message)s").format(record)
    assert "CANARY" not in second
    assert record.exc_text == "Authorization: Basic CANARY_CACHED"


@pytest.mark.parametrize("use_colors", [False, True])
def test_uvicorn_access_formatter_wrapped_without_changing_args(use_colors):
    original = AccessFormatter('%(client_addr)s - "%(request_line)s" %(status_code)s', use_colors=use_colors)
    formatter = CredentialSafeFormatter.wrap(original)
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1:1234", "POST", "/path?token=CANARY_ACCESS", "1.1", 200),
        None,
    )
    args = record.args
    for _ in range(2):
        output = formatter.format(record)
        assert "CANARY" not in output
        assert "127.0.0.1:1234" in output and "POST" in output and "200" in output
    assert record.args is args and record.exc_text is None
    assert CredentialSafeFormatter.wrap(formatter) is formatter
    assert "CANARY" not in CredentialSafeFormatter.wrap(None).format(
        logging.LogRecord("test", logging.INFO, __file__, 1, "password=CANARY_DEFAULT", (), None)
    )


def test_known_capability_and_encoded_values_before_search(tmp_path, monkeypatch):
    secret = "opaque-мій секрет/route-🔑"
    forms = [
        secret,
        quote(secret, safe=""),
        quote(quote(secret, safe=""), safe=""),
        base64.b64encode(secret.encode()).decode(),
        "".join(f"%{byte:02x}" for byte in secret.encode()),
    ]
    values = {secret: "[REDACTED]", "xy": "[REDACTED]"}
    content = "\n".join("2026-11-10 12:00:00,000 ERROR [test] " + form for form in forms)
    monkeypatch.setattr(log_reader.settings, "log_dir", tmp_path)
    (tmp_path / "bamdude.log").write_text(content, encoding="utf-8")
    entries, count = log_reader.read_log_entries(sensitive_strings=values)
    assert count == len(forms) and len(entries) == len(forms)
    for form in forms:
        assert form not in str(entries)
        assert log_reader.read_log_entries(search=form, sensitive_strings=values)[0] == []
    assert "xy" not in log_reader.sanitize_log_content("upstream xy", values)


def test_encoded_camera_url_is_redacted_before_truncation():
    raw = "rtsps://user:CANARY_PASSWORD_" + "x" * 200 + "@host/stream"
    for url in (raw, quote(raw, safe=""), quote(quote(raw, safe=""), safe=""), raw.replace("/", r"\/")):
        safe = (redact_url_credentials(url) or "")[:80]
        assert "CANARY" not in safe and "[REDACTED]" in safe


def test_ffmpeg_query_and_header_redaction_precedes_tail_truncation():
    text = "https://camera/frame?token=" + "x" * 3000 + "CANARY_QUERY_TAIL\n"
    text += "Illegal header value b'CANARY_HEADER'\nDecoder failed"
    for source in (text, quote(text, safe="")):
        summary = summarize_ffmpeg_stderr(source)
        assert "CANARY" not in summary
        assert "Decoder failed" in summary


def test_oid_firmware_hash_and_unicode_survive_but_real_ips_do_not():
    safe = "OID 1.2.840.113556.1.4.1941 firmware 01.09.01.00 SHA256 " + "ab12" * 16 + " діагностика"
    raw = safe + " 192.168.1.2 10.0.0.1:443 http://172.16.0.2/"
    sanitized = log_reader.sanitize_log_content(raw)
    assert safe in sanitized
    assert "192.168.1.2" not in sanitized and "10.0.0.1" not in sanitized and "172.16.0.2" not in sanitized
    assert _sanitize_recursive({"nested": [raw]}, {}) == {"nested": [sanitized]}
    assert log_reader.sanitize_log_content("OID 1.3.6.1.4.1", {"1.3.6.1": "[IP]"}) == "OID 1.3.6.1.4.1"
    assert _sanitize_recursive({"nested": {"password": "opaque", "token": ["opaque"]}}, {}) == {
        "nested": {"password": "[REDACTED]", "token": "[REDACTED]"}
    }
    assert _sanitize_recursive({"has_password": True, "token": None}, {}) == {"has_password": True, "token": None}


def test_snapshot_redacts_across_copy_boundaries_and_omits_oversized_entry():
    prefix = b"2026-11-10 12:00:00,000 ERROR [test] "
    raw = prefix + b"a" * 65500 + b"\nAuthorization: Bearer CANARY_CONTINUATION\n"
    raw += prefix + b"password=CANARY_OVERSIZED" + b"z" * 70000 + b"\n"
    raw += prefix + b"useful diagnostic\n"
    target = io.BytesIO()
    length = log_reader.write_sanitized_log_snapshot(io.BytesIO(raw), target, len(raw), {})
    result = target.getvalue()
    assert length == len(result) and b"CANARY" not in result
    assert b"useful diagnostic" in result and b"omitted oversized" in result
