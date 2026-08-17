"""Attaching an MQTT recording to the support bundle.

⚠️ Never automatic. Everything else in the ZIP is sanitised — the bundle says
so in its own UI and in the text the relay posts to GitHub — but a raw capture
carries the printer's serial in the topic itself and LAN addresses in
``net.info``. So it is opt-in per printer, and these tests pin that the default
really is off.


⚠️ ``db_session`` is taken by every test purely to bring the ORM up: the bundle
collector touches Printer, whose mapper cannot configure without every model
imported.
"""

from __future__ import annotations

import io
import zipfile
from unittest.mock import AsyncMock, patch

import pytest

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


def _isolate_collector(monkeypatch, test_engine):
    """Point the collector at the test database and stop it probing the LAN.

    ⚠️ Both halves are required. ``_collect_support_info`` opens its OWN
    ``async_session``, so without the first it reads the real database — the
    operator's actual printers — and its ``diagnostics`` section then runs
    ``run_connection_diagnostic`` against them over the network. A test has no
    business touching either.
    """
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

    from backend.app.api.routes import support

    monkeypatch.setattr(
        support, "async_session", async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    )
    # Patched at its source: support.py imports it inside the function, so the
    # name does not exist on that module.
    monkeypatch.setattr(
        "backend.app.services.diagnostic_snapshot.collect_diagnostic_snapshot",
        AsyncMock(return_value={}),
    )


def _names(content: bytes) -> list[str]:
    return zipfile.ZipFile(io.BytesIO(content)).namelist()


async def _bundle(query: str = "") -> bytes:
    """Call the route directly. ``_check_port`` is patched: it opens a TCP
    connection to every printer, and a test has no business reaching the LAN."""
    from backend.app.api.routes import support

    with patch.object(support, "_check_port", AsyncMock(return_value=False)):
        response = await support.generate_support_bundle(mqtt_printer_ids=query)

    # The route streams the ZIP, so the bytes come from the iterator rather than
    # a ``.body`` attribute.
    if hasattr(response, "body_iterator"):
        chunks = [chunk async for chunk in response.body_iterator]
        return b"".join(c if isinstance(c, bytes) else c.encode() for c in chunks)
    return response.body


@pytest.fixture
def recording(tmp_path, monkeypatch):
    """A recording on disk, registered with the recorder."""
    from backend.app.services.mqtt_recorder import mqtt_recorder

    path = tmp_path / "mqtt" / "mqtt-20260817-3.log"
    path.parent.mkdir(parents=True)
    path.write_text("2026-08-17T00:00:00\tdevice/X/report\t{}\n", encoding="utf-8")
    monkeypatch.setattr(mqtt_recorder, "_files", {3: path})
    return path


@pytest.fixture
def debug_on(monkeypatch):
    """The bundle refuses to build unless debug logging is on."""
    from backend.app.api.routes import support

    monkeypatch.setattr(support, "_get_debug_setting", AsyncMock(return_value=(True, None)))


async def test_a_recording_is_not_included_by_default(recording, debug_on, db_session, test_engine, monkeypatch):
    """The load-bearing assertion: silence means nothing raw was added."""
    _isolate_collector(monkeypatch, test_engine)
    _isolate_collector(monkeypatch, test_engine)
    content = await _bundle()

    assert not any(n.startswith("mqtt/") for n in _names(content))


async def test_a_recording_is_included_when_asked_for(recording, debug_on, db_session, test_engine, monkeypatch):
    _isolate_collector(monkeypatch, test_engine)
    content = await _bundle("3")

    assert any(n.startswith("mqtt/") for n in _names(content))


async def test_a_printer_with_no_recording_is_simply_skipped(recording, debug_on, db_session, test_engine, monkeypatch):
    """Asking for one that does not exist is not an error — the operator may
    have stopped it between opening the dialog and pressing the button."""
    _isolate_collector(monkeypatch, test_engine)
    content = await _bundle("3,99")

    assert len([n for n in _names(content) if n.startswith("mqtt/")]) == 1


async def test_a_malformed_parameter_does_not_cost_the_bundle(
    recording, debug_on, db_session, test_engine, monkeypatch
):
    """A bad query string must not stand between somebody and their support
    bundle — the rest of the ZIP is still what they came for."""
    _isolate_collector(monkeypatch, test_engine)
    content = await _bundle("not-a-number,,3")

    assert "support-info.json" in _names(content)
    assert any(n.startswith("mqtt/") for n in _names(content))
