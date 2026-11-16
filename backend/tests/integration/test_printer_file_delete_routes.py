"""The file browser's delete doors refuse the file a job reads (G10 B, M07/M08)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.services import printer_file_guard as guard
from backend.app.services.bambu_ftp import DeleteResult

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
def observed(monkeypatch):
    """What the guard sees of the printer; the test changes it."""
    box = SimpleNamespace(
        state=SimpleNamespace(connected=True, state="IDLE", gcode_file=None, subtask_name=None),
        fresh=True,
        expected={},
    )
    monkeypatch.setattr(
        guard.printer_manager, "peek_status", lambda _pid: (box.state, 1.0 if box.fresh else None, not box.fresh)
    )
    monkeypatch.setattr(guard, "_expected_archive", lambda _pid, path: box.expected.get(path.rsplit("/", 1)[-1]))
    monkeypatch.setattr(guard, "_printer_expects_a_print", lambda _pid: bool(box.expected))

    def printing(name="Cube", state="RUNNING"):
        box.state = SimpleNamespace(connected=True, state=state, gcode_file=f"/sdcard/{name}.3mf", subtask_name=name)

    box.printing = printing
    return box


def _routes(transport=None):
    manager = MagicMock()
    manager.get_status.return_value = SimpleNamespace(connected=True, state="IDLE", sdcard=True)
    return (
        patch("backend.app.api.routes.printers.printer_manager", manager),
        patch("backend.app.api.routes.printers.transport_for", return_value=transport or SimpleNamespace()),
        patch("backend.app.api.routes.printers._resolve_storage", return_value="external"),
    )


class TestDeleteOneFile:
    async def test_the_file_of_the_running_print_is_refused_before_any_command(
        self, async_client: AsyncClient, printer_factory, observed
    ):
        printer = await printer_factory()
        observed.printing("Cube")
        transport = SimpleNamespace(delete=AsyncMock())
        a, b, c = _routes(transport)
        with a, b, c:
            response = await async_client.delete(f"/api/v1/printers/{printer.id}/files", params={"path": "/Cube.3mf"})
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "active_print"
        transport.delete.assert_not_awaited()

    async def test_another_file_is_deleted_while_a_print_runs(
        self, async_client: AsyncClient, printer_factory, observed
    ):
        printer = await printer_factory()
        observed.printing("Cube")
        transport = SimpleNamespace(delete=AsyncMock(return_value=DeleteResult.DELETED))
        a, b, c = _routes(transport)
        with a, b, c:
            response = await async_client.delete(f"/api/v1/printers/{printer.id}/files", params={"path": "/Benchy.3mf"})
        assert response.status_code == 200
        assert transport.delete.await_args.kwargs["may_run"]() is True

    async def test_a_print_that_starts_while_the_delete_waits_is_answered_as_a_refusal(
        self, async_client: AsyncClient, printer_factory, observed
    ):
        printer = await printer_factory()

        async def delete(path, *, may_run=None):
            observed.printing("Cube", state="PREPARE")  # began while the request waited for the file channel
            return DeleteResult.DELETED if may_run() else DeleteResult.FAILED

        a, b, c = _routes(SimpleNamespace(delete=delete))
        with a, b, c:
            response = await async_client.delete(f"/api/v1/printers/{printer.id}/files", params={"path": "/Cube.3mf"})
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "active_print"

    async def test_a_printer_nobody_hears_gives_nothing_up(self, async_client: AsyncClient, printer_factory, observed):
        printer = await printer_factory()
        observed.fresh = False
        transport = SimpleNamespace(delete=AsyncMock())
        a, b, c = _routes(transport)
        with a, b, c:
            response = await async_client.delete(f"/api/v1/printers/{printer.id}/files", params={"path": "/Cube.3mf"})
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "printer_state_unknown"
        transport.delete.assert_not_awaited()

    async def test_the_next_prints_file_is_refused(self, async_client: AsyncClient, printer_factory, observed):
        printer = await printer_factory()
        observed.expected = {"Cube.3mf": 7}
        transport = SimpleNamespace(delete=AsyncMock())
        a, b, c = _routes(transport)
        with a, b, c:
            response = await async_client.delete(f"/api/v1/printers/{printer.id}/files", params={"path": "/Cube.3mf"})
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "expected_print"

    async def test_a_real_failure_is_still_a_failure(self, async_client: AsyncClient, printer_factory, observed):
        printer = await printer_factory()
        transport = SimpleNamespace(delete=AsyncMock(return_value=DeleteResult.FAILED))
        a, b, c = _routes(transport)
        with a, b, c:
            response = await async_client.delete(f"/api/v1/printers/{printer.id}/files", params={"path": "/Cube.3mf"})
        assert response.status_code == 500


class TestClearTheCard:
    @pytest.mark.parametrize(
        ("setup", "code"),
        [
            (lambda o: o.printing("Cube", "RUNNING"), "active_print"),
            (lambda o: o.printing("Cube", "PREPARE"), "active_print"),
            (lambda o: o.printing("Cube", "SLICING"), "active_print"),
            (lambda o: setattr(o, "fresh", False), "printer_state_unknown"),
            (lambda o: setattr(o, "expected", {"Cube.3mf": 7}), "expected_print"),
        ],
    )
    async def test_it_is_refused_unless_the_printer_is_freshly_idle(
        self, async_client: AsyncClient, printer_factory, observed, setup, code
    ):
        printer = await printer_factory()
        setup(observed)
        a, b, c = _routes()
        with a, b, c, patch("backend.app.api.routes.printers.clear_sdcard_async", new=AsyncMock()) as clear:
            response = await async_client.post(f"/api/v1/printers/{printer.id}/files/clear-sdcard")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == code
        clear.assert_not_awaited()

    async def test_an_idle_printer_is_cleared_under_a_gate_asked_at_every_step(
        self, async_client: AsyncClient, printer_factory, observed
    ):
        printer = await printer_factory()
        summary = {"deleted": 3, "failed": 0, "folders_removed": 1, "connected": True, "stopped": False}
        a, b, c = _routes()
        with (
            a,
            b,
            c,
            patch("backend.app.api.routes.printers.clear_sdcard_async", new=AsyncMock(return_value=summary)) as clear,
        ):
            response = await async_client.post(f"/api/v1/printers/{printer.id}/files/clear-sdcard")
        assert response.status_code == 200
        assert response.json() == {
            "status": "cleared",
            "deleted": 3,
            "failed": 0,
            "folders_removed": 1,
            "stopped": False,
        }
        may_run = clear.await_args.kwargs["may_run"]
        assert may_run() is True
        observed.expected = {"Cube.3mf": 7}
        assert may_run() is False

    async def test_a_walk_a_new_print_stopped_says_what_it_did(
        self, async_client: AsyncClient, printer_factory, observed
    ):
        printer = await printer_factory()
        summary = {"deleted": 2, "failed": 0, "folders_removed": 0, "connected": True, "stopped": True}
        a, b, c = _routes()
        with a, b, c, patch("backend.app.api.routes.printers.clear_sdcard_async", new=AsyncMock(return_value=summary)):
            response = await async_client.post(f"/api/v1/printers/{printer.id}/files/clear-sdcard")
        assert response.status_code == 200
        assert response.json()["status"] == "stopped"
        assert response.json()["deleted"] == 2

    async def test_a_walk_stopped_before_its_first_deletion_is_a_refusal(
        self, async_client: AsyncClient, printer_factory, observed
    ):
        printer = await printer_factory()

        async def clear(*_a, may_run=None, **_k):
            observed.printing("Cube", "PREPARE")
            return {"deleted": 0, "failed": 0, "folders_removed": 0, "connected": True, "stopped": not may_run()}

        a, b, c = _routes()
        with a, b, c, patch("backend.app.api.routes.printers.clear_sdcard_async", new=clear):
            response = await async_client.post(f"/api/v1/printers/{printer.id}/files/clear-sdcard")
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "active_print"


class TestDeleteTheOldestRecording:
    """The printer picks the file itself, so it is a bulk deletion."""

    def _client(self):
        client = MagicMock()
        client.state = SimpleNamespace(connected=True)
        client.delete_oldest_timelapse.return_value = True
        return client

    async def _post(self, async_client, printer, client):
        manager = MagicMock()
        manager.get_client.return_value = client
        with (
            patch("backend.app.api.routes.printers.printer_manager", manager),
            patch(
                "backend.app.api.routes.printers.timelapse_capability_for",
                return_value={"supports_internal": True, "default": "internal"},
            ),
            patch("backend.app.api.routes.printers._timelapse_target", return_value="internal"),
        ):
            return await async_client.post(
                f"/api/v1/printers/{printer.id}/timelapse/delete-oldest", params={"total_layer": 10}
            )

    async def test_it_is_refused_while_a_print_is_loaded(self, async_client: AsyncClient, printer_factory, observed):
        printer = await printer_factory()
        observed.printing("Cube", "RUNNING")
        client = self._client()
        response = await self._post(async_client, printer, client)
        assert response.status_code == 409
        assert response.json()["detail"]["error"] == "active_print"
        client.delete_oldest_timelapse.assert_not_called()

    async def test_it_runs_on_a_freshly_idle_printer(self, async_client: AsyncClient, printer_factory, observed):
        printer = await printer_factory()
        client = self._client()
        response = await self._post(async_client, printer, client)
        assert response.status_code == 200
        client.delete_oldest_timelapse.assert_called_once()
