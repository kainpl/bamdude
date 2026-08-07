"""Credentials must not reach the log through subprocess output (upstream #N + #2593 hardening).

ffmpeg echoes its input URL back in the ``Input #0`` banner line, and we log its
stderr. Our RTSP URL is ``rtsp://bblp:<access code>@…`` — so the printer's access
code was written to ``bamdude.log`` in plaintext, on disk, where the
support-bundle sanitizer (which only runs on the way *out*) could not help.

We had already understood the risk on the *argv* side — ``routes/camera.py``
builds a ``_redacted_cmd`` before logging the command. The stderr path was simply
never considered.

One pattern now serves both consumers, so they cannot drift: the live log keeps
the username for diagnosis, the support bundle drops it.
"""

from __future__ import annotations

import time

from backend.app.api.routes.camera import _summarize_ffmpeg_stderr
from backend.app.core.logging_filters import URL_CREDENTIALS_PATTERN, redact_url_credentials
from backend.app.services.log_reader import sanitize_log_content

# The reported shape, verbatim from ffmpeg's banner.
FFMPEG_INPUT_LINE = "Input #0, rtsp, from 'rtsp://bblp:12345678@127.0.0.1:8554/streaming/live/1':"


class TestTheReportedLeak:
    def test_ffmpeg_banner_loses_the_access_code(self) -> None:
        out = redact_url_credentials(FFMPEG_INPUT_LINE)
        assert "12345678" not in out
        assert "[REDACTED]" in out
        # Everything that makes the line useful for triage survives.
        assert "Input #0" in out
        assert "bblp" in out
        assert "127.0.0.1:8554/streaming/live/1" in out

    def test_the_stderr_funnel_redacts(self) -> None:
        """``_summarize_ffmpeg_stderr`` is the one funnel every stderr log in the
        camera route passes through — masking there covers all of them at once."""
        assert "12345678" not in _summarize_ffmpeg_stderr(FFMPEG_INPUT_LINE)


class TestTheHolesInTheOldPattern:
    """Both of these were live before this change and neither is hypothetical."""

    def test_ftp_and_ftps_are_redacted(self) -> None:
        """The old sanitizer's scheme allowlist was ``https?|rtsps?``, so FTP was
        never redacted — and FTPS-to-printer carries the same ``bblp:<code>``."""
        for scheme in ("ftp", "ftps"):
            line = f"{scheme}://bblp:SECRETCODE@192.168.1.5:990/cache/x.3mf"
            assert "SECRETCODE" not in redact_url_credentials(line), scheme
            assert "SECRETCODE" not in sanitize_log_content(line), scheme

    def test_a_password_containing_an_at_sign_leaves_no_tail(self) -> None:
        """The old secret class stopped at the FIRST ``@``. RFC 3986 ends the
        userinfo at the LAST one before the path, so a legal ``@`` in a password
        used to leave its tail in the log."""
        out = redact_url_credentials("http://user:p@ssw0rd@cam.local/stream")
        assert "ssw0rd" not in out
        assert out == "http://user:[REDACTED]@cam.local/stream"


class TestTheTwoMaskingForms:
    def test_the_log_keeps_the_username_and_the_bundle_drops_it(self) -> None:
        line = "rtsps://bblp:CODE@printer.lan:322/streaming/live/1"
        assert redact_url_credentials(line) == "rtsps://bblp:[REDACTED]@printer.lan:322/streaming/live/1"
        assert "bblp" not in sanitize_log_content(line)
        assert "[CREDENTIALS]" in sanitize_log_content(line)

    def test_both_use_the_one_pattern(self) -> None:
        """Pinned so a future edit to either consumer cannot quietly fork the
        definition of 'a URL that carries a secret'."""
        import backend.app.services.log_reader as log_reader

        assert log_reader.URL_CREDENTIALS_PATTERN is URL_CREDENTIALS_PATTERN


class TestOrderingAndNoOps:
    def test_truncating_before_redacting_leaves_the_secret(self) -> None:
        """The trap this helper's docstring warns about.

        The pattern anchors on the ``@`` that ends the userinfo. Cut the string
        between the secret and that ``@`` — which is exactly what a ``[:200]``
        does to a long stderr blob — and the pattern no longer matches, so the
        redaction becomes a silent no-op while the password sits in the log.
        """
        line = "x" * 100 + FFMPEG_INPUT_LINE
        cut = line.index("@")  # slice ends immediately before the anchor

        wrong_order = redact_url_credentials(line[:cut]) or ""
        assert "12345678" in wrong_order, "precondition: truncating first defeats the pattern"

        right_order = (redact_url_credentials(line) or "")[:cut]
        assert "12345678" not in right_order

    def test_nothing_to_mask_returns_the_input_unchanged(self) -> None:
        for value in (None, "", "plain log line", "https://example.com/no-userinfo"):
            assert redact_url_credentials(value) == value

    def test_a_bare_at_sign_without_a_url_is_left_alone(self) -> None:
        line = "notify user@example.com that slot 2 is empty"
        assert redact_url_credentials(line) == line


class TestReDoSBound:
    def test_a_long_scheme_legal_run_does_not_blow_up(self) -> None:
        """The scheme repetition is capped at 63. Unbounded, the match is
        quadratic in subject length — and the subject here is ffmpeg stderr,
        which carries an operator-supplied URL and reaches the pattern before any
        truncation, so its length is attacker-influenced.

        Timing assertions are usually a smell; this one is the actual contract,
        and the bound makes it three orders of magnitude clear of the threshold.
        """
        subject = "a" * 200_000  # scheme-legal, never contains "://"
        start = time.perf_counter()
        redact_url_credentials(subject)
        assert time.perf_counter() - start < 1.0

    def test_a_pseudo_scheme_longer_than_the_bound_is_still_masked(self) -> None:
        """The cap does not create a bypass: the match simply starts from a later
        offset inside the over-long scheme."""
        line = "z" * 80 + "://user:SECRET@host/path"
        assert "SECRET" not in redact_url_credentials(line)
