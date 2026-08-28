"""An oversized model must be explained, not reported as a slicer crash.

Ported from upstream #2802. The sidecar caps model uploads and older images
report the rejection as a bare HTTP 500 "File too large" — multer's error is
not the sidecar's own error type, so its handler falls through to the default
status. A 500 reads as a crash inside the slicer, and the one message about
request size was written for the 413 a reverse proxy sends, so it never
appeared. The reporter tried four env vars that nothing reads, stopped nginx,
and moved from Windows to Docker.

⚠️ Matched by what the sidecar SAYS, not by its status, so an installation
still on an older image gets the same explanation.

⚠️ The 500 match is strict — the body must be *only* multer's message —
because a genuine CLI failure is also a 500 and has to keep reaching the
embedded-settings fallback.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.services.slicer_api import (
    SlicerApiError,
    SlicerApiServerError,
    SlicerInputError,
    _handle_slice_response,
    _transport_error_reason,
    _upload_size_rejection,
)


def _response(status: int, payload: dict | None = None, text: str | None = None) -> httpx.Response:
    if payload is not None:
        return httpx.Response(status, json=payload, request=httpx.Request("POST", "http://sidecar/slice"))
    return httpx.Response(status, text=text or "", request=httpx.Request("POST", "http://sidecar/slice"))


class TestRecognisingTheRejection:
    def test_an_old_sidecars_bare_500(self):
        """The reported case: multer's message under a 500."""
        message = _upload_size_rejection(_response(500, {"message": "File too large"}), 200 * 1024 * 1024)

        assert message is not None
        assert "too large" in message

    def test_a_current_sidecars_413(self):
        message = _upload_size_rejection(
            _response(413, {"message": "Upload limit is 512 MB (MAX_MODEL_UPLOAD_MB)"}), None
        )

        assert message is not None

    def test_the_size_is_named_when_known(self):
        message = _upload_size_rejection(_response(500, {"message": "File too large"}), 200 * 1024 * 1024)

        assert "200 MB" in message

    def test_and_left_out_when_it_is_not(self):
        message = _upload_size_rejection(_response(500, {"message": "File too large"}), None)

        assert "MB model file" not in message


class TestTheAdviceMatchesTheImage:
    def test_a_sidecar_that_names_the_knob_is_told_to_set_it(self):
        message = _upload_size_rejection(_response(413, {"message": "upload limit exceeded"}), None)

        assert "MAX_MODEL_UPLOAD_MB" in message
        assert "rebuilding" in message or "rebuild" in message

    def test_one_that_does_not_is_told_to_rebuild(self):
        """⚠️ An older image has no variable to set, so "set MAX_MODEL_UPLOAD_MB"
        would be advice that cannot be followed."""
        message = _upload_size_rejection(_response(500, {"message": "File too large"}), None)

        assert "predates" in message

    def test_both_rule_out_the_reverse_proxy_first(self):
        """Those are the layers people reach for, because they look like they
        should apply."""
        for response in (_response(500, {"message": "File too large"}), _response(413, {"message": "upload limit"})):
            message = _upload_size_rejection(response, None)
            assert "client_max_body_size" in message
            assert "proxy" in message


class TestWhatMustNotMatch:
    def test_a_genuine_cli_failure_stays_a_server_error(self):
        """⚠️ The whole reason the 500 match is strict. A CLI failure carries
        the slicer's stderr, so it never reduces to the bare string — and it
        must keep reaching the embedded-settings fallback."""
        response = _response(500, {"message": "Slicing failed", "details": "Segmentation fault (core dumped)"})

        assert _upload_size_rejection(response, None) is None

    def test_a_500_that_merely_mentions_the_phrase_is_not_enough(self):
        response = _response(500, {"message": "File too large to slice: out of memory in model loader"})

        assert _upload_size_rejection(response, None) is None

    def test_a_proxys_own_413_keeps_its_own_advice(self):
        """⚠️ A proxy limit really is fixed at the proxy, so it must not be
        answered with "the limit lives inside the sidecar"."""
        response = _response(413, text="<html><title>413 Request Entity Too Large</title></html>")

        assert _upload_size_rejection(response, None) is None

    def test_a_successful_response_is_not_a_rejection(self):
        assert _upload_size_rejection(_response(200, {"ok": True}), None) is None


class TestHowItReachesTheCaller:
    def test_it_is_an_input_error_not_a_server_error(self):
        """⚠️ Load-bearing: SlicerInputError is what stops the library route
        retrying the identical oversized upload with embedded settings — a
        second slow conversion for a guaranteed identical answer."""
        with pytest.raises(SlicerInputError) as caught:
            _handle_slice_response(_response(500, {"message": "File too large"}), export_3mf=True)

        assert "too large" in str(caught.value)
        assert not isinstance(caught.value, SlicerApiServerError)

    def test_a_real_cli_failure_still_raises_a_server_error(self):
        with pytest.raises(SlicerApiServerError):
            _handle_slice_response(
                _response(500, {"message": "Slicing failed", "details": "core dumped"}), export_3mf=True
            )

    def test_both_are_slicer_api_errors(self):
        for response in (
            _response(500, {"message": "File too large"}),
            _response(500, {"message": "Slicing failed", "details": "boom"}),
        ):
            with pytest.raises(SlicerApiError):
                _handle_slice_response(response, export_3mf=True)


class TestATransportFailureAlwaysNamesSomething:
    def test_an_exception_with_no_message_still_reports_its_type(self):
        """⚠️ Several httpx.RequestError subclasses are raised with no args, so
        log lines read "Slicer sidecar unreachable:" and stopped there."""
        exc = httpx.ConnectError("")

        assert _transport_error_reason(exc) == "ConnectError"

    def test_a_real_message_is_preferred(self):
        exc = httpx.ConnectError("Connection refused")

        assert _transport_error_reason(exc) == "Connection refused"
