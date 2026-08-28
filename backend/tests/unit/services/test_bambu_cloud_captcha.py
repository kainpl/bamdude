"""Bambu Cloud's CAPTCHA challenge must be explained, not repeated.

Ported from upstream #2790. A user tried to connect to Bambu Cloud and got "We
need you to confirm you are not a robot" as an error toast, with no CAPTCHA
anywhere to answer and nothing to click. That sentence is Bambu's, not ours:
their anti-abuse layer had flagged the network and was answering the sign-in
with HTTP 418 and a challenge body.

⚠️ The reply is well-formed JSON, so the Cloudflare detector — which fires on an
unparseable body, CF markers, 403+cf-mitigated or 503+cf-ray — never saw it, and
the generic error path lifted Bambu's own wording out and handed it to the UI
verbatim. The user was left to conclude their password was wrong. Four sign-in
attempts inside eighteen seconds appear in the report's log, each one more
evidence for the thing that had flagged them.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

import backend.app.models.printer_location  # noqa: F401
from backend.app.services import bambu_cloud
from backend.app.services.bambu_cloud import (
    captcha_cooloff_active,
    is_captcha_challenge,
    note_captcha_challenge,
)


class _Response:
    """Just enough of an httpx response for the shape test."""

    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


@pytest.fixture(autouse=True)
def _clean_cooloff():
    bambu_cloud._captcha_blocked_until.clear()
    yield
    bambu_cloud._captcha_blocked_until.clear()


class TestRecognisingTheChallenge:
    def test_the_reported_body(self):
        response = _Response(418, {"captchaId": "abc123", "error": "We need you to confirm you are not a robot"})

        assert is_captcha_challenge(response) is True

    def test_the_field_name_alone_is_enough(self):
        """⚠️ Identified by carrying a captchaId at all, whatever it says —
        Bambu has shipped the challenge under more than one wording."""
        assert is_captcha_challenge(_Response(418, {"captchaId": "x"})) is True

    def test_a_different_wording_is_still_caught(self):
        assert is_captcha_challenge(_Response(418, {"error": "Please verify you are human (captcha)"})) is True

    def test_a_challenge_served_as_html_is_caught(self):
        """Not JSON — fall back to the raw body rather than report it as an
        unexplained failure."""
        assert is_captcha_challenge(_Response(418, None, text="<html>captchaId: 42</html>")) is True


class TestNotEveryRefusalIsACaptcha:
    def test_a_bare_418_is_not_reported_as_one(self):
        """⚠️ Telling somebody to solve a challenge that was never offered is
        the exact confusion this exists to end."""
        assert is_captcha_challenge(_Response(418, {"error": "Too many requests"})) is False

    def test_a_401_is_a_wrong_password_whatever_it_says(self):
        assert is_captcha_challenge(_Response(401, {"captchaId": "x"})) is False

    def test_a_success_is_not_a_challenge(self):
        assert is_captcha_challenge(_Response(200, {"accessToken": "t"})) is False

    def test_a_response_with_no_status_does_not_raise(self):
        assert is_captcha_challenge(SimpleNamespace()) is False
        assert is_captcha_challenge(_Response(418, None, text="")) is False


class TestTheCoolOff:
    def test_a_challenge_holds_back_further_sign_ins(self):
        note_captcha_challenge("https://api.bambulab.com")

        assert captcha_cooloff_active("https://api.bambulab.com") is True

    def test_it_is_keyed_per_origin(self):
        """⚠️ The TOTP step posts to bambulab.com while everything else posts to
        api.bambulab.com. A challenge on one must not strand somebody halfway
        through a two-factor sign-in on the other — and the block lives at the
        edge in front of one region, so a challenge on .com says nothing about
        .cn."""
        note_captcha_challenge("https://api.bambulab.com")

        assert captcha_cooloff_active("https://bambulab.com") is False
        assert captcha_cooloff_active("https://api.bambulab.cn") is False

    def test_an_entry_expires_and_is_dropped_on_the_way_past(self):
        """So the map cannot grow past one entry per region."""
        bambu_cloud._captcha_blocked_until["https://api.bambulab.com"] = time.monotonic() - 1

        assert captcha_cooloff_active("https://api.bambulab.com") is False
        assert "https://api.bambulab.com" not in bambu_cloud._captcha_blocked_until

    def test_an_origin_never_challenged_is_not_held(self):
        assert captcha_cooloff_active("https://api.bambulab.com") is False


@pytest.mark.asyncio
class TestWhatTheSignInReturns:
    async def test_every_sign_in_call_refuses_while_the_cool_off_holds(self):
        """⚠️ Not to wait the block out — it lasts hours — but to stop us
        deepening it while the user reads the explanation."""
        service = bambu_cloud.BambuCloudService(region="global")
        try:
            note_captcha_challenge(service.base_url)

            result = await service.login_request("someone@example.com", "hunter2")

            assert result["success"] is False
            assert result["reason"] == "captcha"
            assert "CAPTCHA" in result["message"]
        finally:
            await service.close()

    async def test_the_reason_is_what_the_ui_branches_on(self):
        """The message alone cannot be told apart from a wrong password — which
        is how Bambu's own sentence ended up flashed as a toast the user could
        do nothing about."""
        service = bambu_cloud.BambuCloudService(region="global")
        try:
            note_captcha_challenge(service.base_url)

            for result in (
                await service.login_request("a@b.c", "pw"),
                await service.verify_code("a@b.c", "123456"),
            ):
                assert result["reason"] == "captcha"
                assert result["needs_verification"] is False
        finally:
            await service.close()


class TestTheHealthScannerSeesIt:
    def test_a_signature_matches_the_log_line(self):
        """The reported bundle came back with zero findings while the log was
        full of the failure."""
        import re

        from backend.app.services.log_health import SIGNATURES

        signature = next((s for s in SIGNATURES if s.id == "bambu-cloud-captcha"), None)
        assert signature is not None

        line = (
            "Bambu Cloud is challenging this network with a CAPTCHA (HTTP 418 from "
            "https://api.bambulab.com). Sign-in cannot complete until the challenge clears"
        )
        assert any(re.search(p, line) for p in signature.patterns)


class TestTheTotpStepUsesItsOwnOrigin:
    """⚠️ Asserted structurally, and deliberately.

    The per-origin behaviour of the cool-off map is covered above, but that
    proves nothing about which origin ``verify_totp`` hands it — and that is the
    half that strands somebody mid-two-factor. Driving the real call needs the
    CSRF fetch and the TFA POST mocked; the guarantee is one argument, so it is
    asserted where it lives.
    """

    @staticmethod
    def _source() -> str:
        import inspect

        from backend.app.services.bambu_cloud import BambuCloudService

        return inspect.getsource(BambuCloudService.verify_totp)

    def test_the_hold_is_keyed_on_the_web_origin(self):
        assert "self._captcha_cooloff_holds(web_origin)" in self._source()

    def test_and_so_is_the_challenge_it_records(self):
        assert "self._note_captcha(response, web_origin)" in self._source()

    def test_neither_falls_back_to_the_api_base(self):
        source = self._source()
        assert "self._captcha_cooloff_holds()" not in source
        assert "self._note_captcha(response)" not in source
