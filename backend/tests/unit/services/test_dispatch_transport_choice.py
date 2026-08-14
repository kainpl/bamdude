"""Which medium a dispatch uses, and when it refuses instead.

⚠️ The tunnel carries a print ONLY when the printer reports no card. A healthy
card always means FTP; a damaged or read-only one refuses and does not fall
back — BambuStudio's own gate, and the rule this whole feature was scoped by.
"""

import asyncio

import pytest

from backend.app.services.background_dispatch import (
    _dispatch_refusal_message,
    _upload_failure_message,
    delete_internal_by_name,
    resolve_dispatch_storage,
)
from backend.app.utils.timelapse import (
    SDCARD_ABNORMAL,
    SDCARD_NONE,
    SDCARD_NORMAL,
    SDCARD_READONLY,
)


def _state(card: int, *, emmc: bool = True, internal: bool = True):
    return type(
        "S",
        (),
        {
            "sdcard_state": card,
            "print_option_support": {"print_with_emmc": emmc, "model_internal_storage": internal},
        },
    )()


def test_a_healthy_card_always_means_ftp():
    assert resolve_dispatch_storage("X2D", _state(SDCARD_NORMAL)) == ("external", None)


def test_no_card_on_a_machine_with_internal_storage_means_the_tunnel():
    assert resolve_dispatch_storage("X2D", _state(SDCARD_NONE)) == ("internal", None)


def test_no_card_and_no_internal_storage_refuses_with_a_reason():
    assert resolve_dispatch_storage("P1S", _state(SDCARD_NONE, emmc=False, internal=False)) == (
        None,
        "no_card_no_internal",
    )


@pytest.mark.parametrize("card", [SDCARD_ABNORMAL, SDCARD_READONLY])
def test_a_damaged_card_refuses_rather_than_falling_back(card):
    """⚠️ Even on a machine with eMMC. A card the printer cannot read means
    something is wrong with the machine, and routing around it hides it."""
    assert resolve_dispatch_storage("X2D", _state(card)) == (None, "card_unusable")


def test_a_printer_with_no_live_state_is_not_routed_to_a_guess():
    assert resolve_dispatch_storage("X2D", None)[0] == "external"


def test_the_refusal_never_mentions_a_card_to_a_machine_that_needs_none():
    """The old text told every operator to check a card, including on models
    that print from internal storage — advice that cannot be followed."""
    assert "card" in _dispatch_refusal_message("no_card_no_internal").lower()
    assert "card" in _dispatch_refusal_message("card_unusable").lower()
    # An unknown reason must still say something, and not something wrong.
    generic = _dispatch_refusal_message(None)
    assert generic
    assert "sd card" not in generic.lower()


@pytest.mark.asyncio
async def test_the_same_named_file_is_found_by_listing_on_internal_storage():
    """⚠️ The bare name is not a path on internal storage — deleting by name
    would silently do nothing and leave the old file in place, making the whole
    delete-then-upload decision inert on the medium it was written for."""
    from backend.app.services.printer_files.tunnel import TunnelTransport
    from backend.tests.tunnel_fixtures import FakeTunnelServer, listing_entry

    server = FakeTunnelServer()
    server.files = [listing_entry("job.gcode.3mf"), listing_entry("other.gcode.3mf")]
    host, port = await server.start()
    try:

        async def connector():
            return await asyncio.open_connection(host, port)

        printer = type("P", (), {"ip_address": "127.0.0.1", "access_code": "12345678", "model": "X2D"})()
        transport = TunnelTransport(printer, port=port, connector=connector)
        await delete_internal_by_name(transport, "job.gcode.3mf")
        assert server.deleted == ["/userdata/model/history/job.gcode.3mf"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_nothing_is_deleted_when_no_file_of_that_name_is_there():
    from backend.app.services.printer_files.tunnel import TunnelTransport
    from backend.tests.tunnel_fixtures import FakeTunnelServer, listing_entry

    server = FakeTunnelServer()
    server.files = [listing_entry("other.gcode.3mf")]
    host, port = await server.start()
    try:

        async def connector():
            return await asyncio.open_connection(host, port)

        printer = type("P", (), {"ip_address": "127.0.0.1", "access_code": "12345678", "model": "X2D"})()
        transport = TunnelTransport(printer, port=port, connector=connector)
        await delete_internal_by_name(transport, "job.gcode.3mf")
        assert server.deleted == []
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_a_failing_cleanup_never_stops_a_dispatch():
    """A missing file is the normal case, and an unreachable printer will fail
    at the upload with a message of its own. Neither should die here."""

    class _Exploding:
        async def list_files(self, _path, file_type="model"):
            raise OSError("boom")

        async def delete(self, _path):
            raise OSError("boom")

    assert await delete_internal_by_name(_Exploding(), "job.gcode.3mf") is False


@pytest.mark.asyncio
async def test_several_candidate_names_are_tried_and_the_first_hit_wins():
    """Post-print cleanup knows the file by more than one name — the derived
    upload name and the subtask fallback. One listing answers for all of them."""
    from backend.app.services.printer_files.tunnel import TunnelTransport
    from backend.tests.tunnel_fixtures import FakeTunnelServer, listing_entry

    server = FakeTunnelServer()
    server.files = [listing_entry("real-name.gcode.3mf")]
    host, port = await server.start()
    try:

        async def connector():
            return await asyncio.open_connection(host, port)

        printer = type("P", (), {"ip_address": "127.0.0.1", "access_code": "12345678", "model": "X2D"})()
        transport = TunnelTransport(printer, port=port, connector=connector)
        removed = await delete_internal_by_name(transport, "guess.gcode.3mf", "real-name.gcode.3mf")
        assert removed is True
        assert server.deleted == ["/userdata/model/history/real-name.gcode.3mf"]
    finally:
        await server.stop()


@pytest.mark.asyncio
async def test_no_names_means_no_listing_at_all():
    """Cleanup with nothing to look for must not open a connection."""

    class _Watching:
        def __init__(self):
            self.listed = False

        async def list_files(self, _path, file_type="model"):
            self.listed = True
            return []

        async def delete(self, _path):
            raise AssertionError("must not delete")

    watcher = _Watching()
    assert await delete_internal_by_name(watcher) is False
    assert await delete_internal_by_name(watcher, "", None) is False
    assert watcher.listed is False


def test_the_upload_failure_message_only_mentions_a_card_where_one_was_used():
    """⚠️ A failure after the medium was chosen is a different story from a
    refusal before it. Advising an operator to check a card slot the print
    never went near sends them looking for a fault that is not there."""
    assert "sd card" in _upload_failure_message("external").lower()
    internal = _upload_failure_message("internal")
    assert "card" not in internal.lower()
    assert "internal storage" in internal.lower()
