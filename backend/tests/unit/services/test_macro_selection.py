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
