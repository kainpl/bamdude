"""Where the per-print macro selection lives between dispatch and firing.

Two stores, because one window is not covered by the other:
``fire_event_macros("print_started")`` runs at main.py:2857 while the archive
is created around main.py:3234 — four hundred lines later in the same handler
— so at firing time there is often no archive yet. And the archive is the only
copy that survives a restart between print start and print finish.
"""

import pytest

import backend.app.models.printer_location  # noqa: F401 — Printer relates to it by name
from backend.app import main
from backend.app.models.archive import PrintArchive


@pytest.fixture(autouse=True)
def _clean_registry():
    main._active_macro_selection.clear()
    yield
    main._active_macro_selection.clear()


class TestTheInMemoryRegistration:
    def test_a_selection_is_registered_for_the_printer(self) -> None:
        main.register_macro_selection(3, {"selected_macro_ids": [7, 9]})
        assert main._active_macro_selection[3] == [7, 9]

    def test_an_empty_selection_is_still_registered(self) -> None:
        """ "The operator ticked nothing" and "no dispatch happened" must not be
        the same state in memory — only the reader collapses them."""
        main.register_macro_selection(3, {"selected_macro_ids": []})
        assert main._active_macro_selection[3] == []

    def test_options_without_the_key_register_nothing(self) -> None:
        main.register_macro_selection(3, {})
        assert 3 not in main._active_macro_selection

    def test_clearing_forgets_it(self) -> None:
        main.register_macro_selection(3, {"selected_macro_ids": [7]})
        main.clear_macro_selection(3)
        assert 3 not in main._active_macro_selection


class TestTheArchiveCopy:
    def test_the_ids_land_in_extra_data(self) -> None:
        from backend.app.services.archive import set_selected_macro_ids

        archive = PrintArchive()
        archive.extra_data = {"notes": "keep me"}
        set_selected_macro_ids(archive, [7, 9])

        assert archive.extra_data["selected_macro_ids"] == [7, 9]
        assert archive.extra_data["notes"] == "keep me"

    def test_none_writes_nothing(self) -> None:
        from backend.app.services.archive import set_selected_macro_ids

        archive = PrintArchive()
        archive.extra_data = {}
        set_selected_macro_ids(archive, None)

        assert "selected_macro_ids" not in archive.extra_data

    def test_an_empty_list_is_recorded(self) -> None:
        from backend.app.services.archive import set_selected_macro_ids

        archive = PrintArchive()
        archive.extra_data = None
        set_selected_macro_ids(archive, [])

        assert archive.extra_data["selected_macro_ids"] == []


class TestResolution:
    """Memory first, archive second, nothing third."""

    @pytest.mark.asyncio
    async def test_memory_wins_when_present(self, db_session) -> None:
        from backend.app.services.macro_trigger import _selected_macro_ids

        main.register_macro_selection(3, {"selected_macro_ids": [7]})
        assert await _selected_macro_ids(db_session, 3) == {7}

    @pytest.mark.asyncio
    async def test_the_archive_answers_after_a_restart(self, db_session) -> None:
        """A restart between print start and print finish empties the registry;
        the archive is what is left."""
        from backend.app.services.macro_trigger import _selected_macro_ids

        archive = PrintArchive(printer_id=3, filename="w.3mf", file_path="", file_size=0, status="printing")
        archive.extra_data = {"selected_macro_ids": [9]}
        db_session.add(archive)
        await db_session.flush()

        assert await _selected_macro_ids(db_session, 3) == {9}

    @pytest.mark.asyncio
    async def test_nothing_anywhere_is_an_empty_set(self, db_session) -> None:
        from backend.app.services.macro_trigger import _selected_macro_ids

        assert await _selected_macro_ids(db_session, 3) == set()

    @pytest.mark.asyncio
    async def test_an_empty_registration_is_not_a_missing_one(self, db_session) -> None:
        """Both fire nothing, but the empty one must not fall through to an
        archive left over from an earlier print."""
        from backend.app.services.macro_trigger import _selected_macro_ids

        archive = PrintArchive(printer_id=3, filename="w.3mf", file_path="", file_size=0, status="printing")
        archive.extra_data = {"selected_macro_ids": [9]}
        db_session.add(archive)
        await db_session.flush()

        main.register_macro_selection(3, {"selected_macro_ids": []})
        assert await _selected_macro_ids(db_session, 3) == set()


# Helpers copied from test_macro_layer_trigger.py — same shape, different question.
import json  # noqa: E402
from unittest.mock import MagicMock  # noqa: E402

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # noqa: E402

from backend.app.models.macro import Macro  # noqa: E402
from backend.app.services import macro_trigger  # noqa: E402
from backend.app.services.macro_matcher import LAYER_REACHED_EVENT  # noqa: E402


def _running_client(state: str = "RUNNING", sub_stage: int = 0) -> MagicMock:
    client = MagicMock()
    client.state.connected = True
    client.state.state = state
    client.state.mc_print_sub_stage = sub_stage
    return client


def _manager(client) -> MagicMock:
    pm = MagicMock()
    pm.get_client.return_value = client
    return pm


@pytest.fixture
def dispatched(monkeypatch):
    """Collect what would have been spawned instead of running it."""
    calls: list[str] = []
    monkeypatch.setattr(
        macro_trigger,
        "spawn_background_task",
        lambda coro, name=None: (coro.close(), calls.append(name))[1],
    )
    macro_trigger._fired_layer_macros.clear()
    yield calls
    macro_trigger._fired_layer_macros.clear()


async def _a_layer_macro(db, printer_id: int, layer: int = 50) -> int:
    macro = Macro(
        name=f"silent from {layer}",
        printer_models=json.dumps(["*"]),
        swap_mode_only=False,
        swap_profile=None,
        event=LAYER_REACHED_EVENT,
        action_type="mqtt_action",
        mqtt_action="print_speed",
        mqtt_action_param="1",
        trigger_layer=layer,
        delay_seconds=0,
        gcode="",
        enabled=True,
        is_custom=True,
    )
    db.add(macro)
    await db.commit()
    await db.refresh(macro)
    return macro.id


@pytest.mark.asyncio
class TestOnlyTickedMacrosFire:
    async def test_a_ticked_layer_macro_fires(self, test_engine, db_session, printer_factory, dispatched):
        printer = await printer_factory(name="P1", model="P1S")
        macro_id = await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        main.register_macro_selection(printer.id, {"selected_macro_ids": [macro_id]})

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, _manager(_running_client()))

        assert dispatched == [f"macro-layer-{macro_id}"]

    async def test_an_unticked_layer_macro_does_not(self, test_engine, db_session, printer_factory, dispatched):
        printer = await printer_factory(name="P1", model="P1S")
        await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
        main.register_macro_selection(printer.id, {"selected_macro_ids": []})

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, _manager(_running_client()))

        assert dispatched == []

    async def test_a_print_with_no_selection_fires_nothing(self, test_engine, db_session, printer_factory, dispatched):
        """The external / telegram / VP case, and every item queued before this
        feature existed."""
        printer = await printer_factory(name="P1", model="P1S")
        await _a_layer_macro(db_session, printer.id)
        factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        await macro_trigger.fire_layer_macros(printer.id, 50, 49, factory, _manager(_running_client()))

        assert dispatched == []
