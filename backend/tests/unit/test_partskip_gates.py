"""Skipping a part needs two permissions, and the server asked for neither.

Audit item 18. BS gates the Skip Objects dialog on **both**:

* ``is_support_partskip`` — ``fun`` bit 49, i.e. this printer can skip parts;
* ``is_model_support_partskip`` — the sliced plate labelled its objects, so the
  g-code carries markers to skip by (``PartSkipDialog.cpp`` via
  ``GetLabelObjectEnabled``).

We decoded neither on the server. The plate half existed, in the frontend only —
the same shape as the drying ceiling: the UI knew, the endpoint did not, and an
API key got the answer the UI would never have offered. The printer half was not
decoded at all, so ``fun`` bit 49 went past unread.

Firmware's reply to a skip it cannot perform is silence, which is why this had
to be measured against BS rather than against the printer.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from backend.app.services.bambu_mqtt import BambuMQTTClient


def _client() -> BambuMQTTClient:
    return BambuMQTTClient(ip_address="192.168.1.100", serial_number="TESTSERIAL", access_code="12345678", model="H2D")


class TestBitFortyNineIsDecoded:
    def test_a_printer_that_supports_partskip_reports_true(self) -> None:
        c = _client()
        c._parse_print_option_support({"fun": f"{1 << 49:016x}"})

        assert c.state.print_option_support["partskip"] is True

    def test_a_printer_that_does_not_reports_false(self) -> None:
        c = _client()
        c._parse_print_option_support({"fun": "0000000000000000"})

        assert c.state.print_option_support["partskip"] is False

    def test_bit_49_and_not_a_neighbour(self) -> None:
        """Off-by-one in a 64-bit field is silent: the wrong bit answers a
        different question and looks exactly as plausible."""
        c = _client()
        c._parse_print_option_support({"fun": f"{(1 << 48) | (1 << 50):016x}"})

        assert c.state.print_option_support["partskip"] is False

    def test_a_printer_that_never_sent_fun_says_nothing(self) -> None:
        """Absent is not False. The endpoint refuses only on an explicit False,
        so a printer we have not heard the word from keeps working."""
        c = _client()
        c._parse_print_option_support({})

        assert "partskip" not in (c.state.print_option_support or {})


class TestTheSendPathStillOnlyPublishes:
    """The endpoint owns the refusals; the client owns the wire format. Keeping
    the gate out of ``skip_objects()`` is deliberate — it has no printer model
    to ask and no HTTP status to answer with."""

    def test_it_publishes_bs_wire_format(self) -> None:
        import json

        c = _client()
        c._client = MagicMock()
        c.state.connected = True
        c.state.state = "RUNNING"

        assert c.skip_objects([941, 942]) is True

        payload = json.loads(c._client.publish.call_args[0][1])
        assert payload["print"]["command"] == "skip_objects"
        assert payload["print"]["obj_list"] == [941, 942]
