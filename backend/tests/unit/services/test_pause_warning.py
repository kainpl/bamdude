"""The "pause coming up" heads-up: once per pause per run, at the lead time, never at 0."""

import json
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from backend.app import main
from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS
from backend.app.models.telegram_chat import ALL_NOTIFY_EVENTS, DEFAULT_NOTIFY_EVENTS
from backend.app.schemas.notification import NotificationProviderCreate
from backend.app.schemas.settings import AppSettings, AppSettingsUpdate
from backend.app.services import pause_warning
from backend.app.services.notification_events import EVENT_CATALOG, default_inbox_events
from backend.app.services.notification_service import NotificationService, notification_service
from backend.app.services.print_run_binding import claim_print_start
from backend.tests.unit.services.test_layer_projection import push
from backend.tests.unit.services.test_pause_forecast import analysed, bind, rig  # noqa: F401 -- shared rig

DATA = Path(__file__).resolve().parents[3] / "app" / "data"


@pytest.fixture
def warn(monkeypatch):
    """The heads-up with its setting and printer read replaced; what was sent is recorded."""
    pause_warning.reset()
    sent = AsyncMock()
    lead = {"minutes": 5}

    async def load(_db, _printer_id):
        return lead["minutes"], "Farm-3"

    @asynccontextmanager
    async def session():
        yield None

    monkeypatch.setattr(pause_warning, "_load", load)
    monkeypatch.setattr(pause_warning, "async_session", session)
    monkeypatch.setattr(notification_service, "on_pause_coming_up", sent)
    yield type("Warn", (), {"sent": sent, "lead": lead})
    pause_warning.reset()


async def observe(r):
    task = pause_warning.observe(r.m, 3)
    if task is not None:
        await task


def test_the_event_is_catalogued_off_by_default_everywhere():
    assert (EVENT_CATALOG["pause_coming_up"].severity, EVENT_CATALOG["pause_coming_up"].group) == ("info", "print")
    assert "pause_coming_up" not in default_inbox_events()
    assert PROVIDER_EVENT_DEFAULTS["on_pause_coming_up"] is False
    assert NotificationProviderCreate(name="n", provider_type="ntfy", config={}).on_pause_coming_up is False
    assert "pause_coming_up" in ALL_NOTIFY_EVENTS
    assert "pause_coming_up" not in DEFAULT_NOTIFY_EVENTS


def test_both_templates_name_the_print_the_printer_the_layer_and_the_time():
    for language in ("en", "uk"):
        body = json.loads((DATA / f"notification_templates_{language}.json").read_text(encoding="utf-8"))[
            "pause_coming_up"
        ]["body_template"]
        for variable in ("{printer}", "{filename}", "{layer}", "{time_to_pause}"):
            assert variable in body, (language, variable)


def test_the_lead_time_setting_defaults_to_five_and_zero_switches_the_event_off():
    assert AppSettings().pause_warning_minutes == 5
    assert AppSettingsUpdate(pause_warning_minutes=0).pause_warning_minutes == 0
    assert AppSettingsUpdate(pause_warning_minutes=120).pause_warning_minutes == 120
    for bad in (-1, 121):
        with pytest.raises(ValueError):
            AppSettingsUpdate(pause_warning_minutes=bad)


@pytest.mark.asyncio
async def test_it_fires_once_when_the_estimate_reaches_the_lead_time(rig, warn):  # noqa: F811
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=3, mc_remaining_time=40)  # 10 min to the pause at layer 5
    await observe(r)
    assert warn.sent.await_count == 0

    push(r.c, layer_num=4, mc_remaining_time=35)  # 5 min: the lead time
    await observe(r)
    assert warn.sent.await_count == 1
    kwargs = warn.sent.await_args.kwargs
    assert (kwargs["printer_id"], kwargs["printer_name"], kwargs["layer"], kwargs["minutes"]) == (3, "Farm-3", 5, 5)
    assert kwargs["total_layers"] == 12 and kwargs["filename"]

    push(r.c, mc_remaining_time=32)
    await observe(r)
    await observe(r)
    assert warn.sent.await_count == 1


@pytest.mark.asyncio
async def test_each_pause_of_the_run_is_warned_about_once(rig, warn):  # noqa: F811
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=4, mc_remaining_time=33)
    await observe(r)
    push(r.c, layer_num=5, mc_remaining_time=30)  # standing on the pause: the next one is 20 min away
    await observe(r)
    assert warn.sent.await_count == 1
    push(r.c, layer_num=8, mc_remaining_time=14)
    await observe(r)
    await observe(r)
    assert [call.kwargs["layer"] for call in warn.sent.await_args_list] == [5, 9]


@pytest.mark.asyncio
async def test_a_lead_time_of_zero_never_fires(rig, warn):  # noqa: F811
    r = rig
    warn.lead["minutes"] = 0
    bind(r)
    await analysed(r)
    push(r.c, layer_num=4, mc_remaining_time=30)  # 0 min to the pause
    await observe(r)
    assert warn.sent.await_count == 0


@pytest.mark.asyncio
async def test_a_longer_lead_time_fires_earlier(rig, warn):  # noqa: F811
    r = rig
    warn.lead["minutes"] = 15
    bind(r)
    await analysed(r)
    push(r.c, layer_num=2, mc_remaining_time=44)  # 14 min
    await observe(r)
    assert warn.sent.await_args.kwargs["minutes"] == 14


@pytest.mark.asyncio
async def test_without_a_time_it_fires_on_the_layer_before_the_pause(rig, warn):  # noqa: F811
    r = rig
    r.points["value"] = ((5, None), (9, None))
    bind(r)
    await analysed(r)
    push(r.c, layer_num=3, mc_remaining_time=40)
    await observe(r)
    assert warn.sent.await_count == 0
    push(r.c, layer_num=4)
    await observe(r)
    await observe(r)
    assert warn.sent.await_count == 1
    assert (warn.sent.await_args.kwargs["layer"], warn.sent.await_args.kwargs["minutes"]) == (5, None)


@pytest.mark.asyncio
async def test_a_reprint_is_a_new_run_and_warns_again(rig, warn):  # noqa: F811
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=4, mc_remaining_time=33)
    await observe(r)
    assert warn.sent.await_count == 1

    # The same file printed again: another archive incarnation, another observed run.
    events = []
    r.c.on_print_start = events.append
    push(r.c, gcode_state="FINISH", subtask_id="200")
    push(r.c, gcode_state="IDLE", subtask_id="300")
    push(r.c, gcode_state="RUNNING", gcode_file="fixture.gcode", subtask_id="300", layer_num=0, mc_percent=1)
    r.data = events[-1]
    r.generation = claim_print_start(r.m, 3, r.data).generation
    r.a.id, r.a.recovery_identity, r.a.subtask_id = 1264, "the-reprint", "300"
    bind(r)
    await analysed(r)
    push(r.c, layer_num=4, mc_remaining_time=33, total_layer_num=12)
    await observe(r)
    assert [call.kwargs["layer"] for call in warn.sent.await_args_list] == [5, 5]


@pytest.mark.asyncio
async def test_a_print_that_moved_on_during_the_await_is_not_warned_about(rig, warn, monkeypatch):  # noqa: F811
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=4, mc_remaining_time=33)

    async def load(_db, _printer_id):
        push(r.c, layer_num=5, mc_remaining_time=30)  # the pause was reached meanwhile
        return 5, "Farm-3"

    monkeypatch.setattr(pause_warning, "_load", load)
    await observe(r)
    assert warn.sent.await_count == 0


@pytest.mark.asyncio
async def test_a_run_recovered_inside_the_lead_window_skips_that_one_warning(rig, warn):  # noqa: F811
    """After a restart the in-memory record is gone: one warning may be lost, none is repeated."""
    r = rig
    bind(r, origin="exact_subtask")
    await analysed(r)
    push(r.c, layer_num=4, mc_remaining_time=33)  # already inside the window when first seen
    await observe(r)
    await observe(r)
    assert warn.sent.await_count == 0
    push(r.c, layer_num=8, mc_remaining_time=14)  # the next pause is warned about as usual
    await observe(r)
    assert [call.kwargs["layer"] for call in warn.sent.await_args_list] == [9]


@pytest.mark.asyncio
async def test_a_run_recovered_outside_the_window_warns_normally(rig, warn):  # noqa: F811
    r = rig
    bind(r, origin="exact_subtask")
    await analysed(r)
    push(r.c, layer_num=2, mc_remaining_time=50)
    await observe(r)
    push(r.c, layer_num=4, mc_remaining_time=34)
    await observe(r)
    assert [call.kwargs["layer"] for call in warn.sent.await_args_list] == [5]


@pytest.mark.asyncio
async def test_the_status_callback_observes_every_status(rig, warn, monkeypatch):  # noqa: F811
    r = rig
    bind(r)
    await analysed(r)
    push(r.c, layer_num=4, mc_remaining_time=33)
    seen = []
    monkeypatch.setattr(pause_warning, "observe", lambda manager, printer_id: seen.append((manager, printer_id)))
    monkeypatch.setattr(main.ws_manager, "send_printer_status", AsyncMock())
    monkeypatch.setattr(main, "_repeat_available_for", AsyncMock(return_value=False))
    monkeypatch.setattr(main.mqtt_relay, "on_printer_status", AsyncMock(), raising=False)
    await main.on_printer_status_change(3, r.c.state)
    assert seen == [(r.m, 3)]


@pytest.mark.asyncio
async def test_the_service_sends_it_through_the_ordinary_path():
    service = NotificationService()
    with (
        patch.object(service, "_get_providers_for_event", AsyncMock(return_value=["p"])) as providers,
        patch.object(service, "_build_message_from_template", AsyncMock(return_value=("t", "m"))) as build,
        patch.object(service, "_send_to_providers", AsyncMock()) as send,
        patch("backend.app.i18n.get_language", AsyncMock(return_value="en")),
    ):
        await service.on_pause_coming_up(
            printer_id=3, printer_name="Farm-3", filename="vase.gcode.3mf", layer=5, total_layers=12, minutes=4, db=None
        )
        timed = dict(build.await_args.args[2])
        await service.on_pause_coming_up(
            printer_id=3,
            printer_name="Farm-3",
            filename="vase.gcode.3mf",
            layer=5,
            total_layers=12,
            minutes=None,
            db=None,
        )
        layer_only = dict(build.await_args.args[2])
    assert providers.await_args.args[1:] == ("on_pause_coming_up", 3)
    assert build.await_args.args[1] == "pause_coming_up"
    assert send.await_args.args[4] == "pause_coming_up"
    assert timed == {
        "printer": "Farm-3",
        "filename": "vase",
        "layer": "5",
        "total_layers": "12",
        "minutes": "4",
        "time_to_pause": "in about 4 min",
    }
    assert (layer_only["minutes"], layer_only["time_to_pause"]) == ("", "on the next layer")


@pytest.mark.asyncio
async def test_the_ukrainian_phrases_exist():
    from backend.app.i18n import t

    assert t("uk", "pause_warning", "in_minutes", minutes=4) == "приблизно через 4 хв"
    assert t("uk", "pause_warning", "next_layer") == "на наступному шарі"
