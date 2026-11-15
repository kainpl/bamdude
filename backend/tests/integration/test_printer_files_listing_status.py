"""The printer files route says what its listing proves (G10 A, M03)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.services.printer_files.base import RemoteFile


class _Transport:
    def __init__(self, answer):
        self.answer = answer

    async def list_directory(self, path, file_type="model"):
        return self.answer


def _state():
    state = MagicMock()
    state.connected = True
    return state


@pytest.mark.asyncio
@pytest.mark.integration
@pytest.mark.parametrize(
    ("answer", "status", "files"),
    [
        (("complete", [RemoteFile(name="a.3mf", path="/a.3mf", size=3)], None), "complete", ["a.3mf"]),
        (("complete", [], None), "complete", []),
        (("unreadable", [], "550"), "unreadable", []),
        (("missing", [], "absent from /"), "missing", []),
        (("unavailable", [], "timeout"), "unavailable", []),
    ],
)
async def test_the_listing_status_reaches_the_response(
    async_client: AsyncClient, printer_factory, answer, status, files
):
    from backend.app.services.printer_files.listing import ListingCoalescer

    printer = await printer_factory()
    with (
        patch("backend.app.api.routes.printers.printer_manager") as manager,
        patch("backend.app.api.routes.printers.transport_for", return_value=_Transport(answer)),
        # A fresh coalescer per case: the last complete listing it remembers must not
        # make a later case read as "stale".
        patch("backend.app.services.printer_files.listing.listing_coalescer", ListingCoalescer()),
    ):
        manager.get_status.return_value = _state()
        response = await async_client.get(f"/api/v1/printers/{printer.id}/files", params={"path": "/"})
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == status
    assert [f["name"] for f in body["files"]] == files
    assert body["stale"] is False
