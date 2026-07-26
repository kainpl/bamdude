"""The Spoolman bulk routes fan out over the single-spool ones (#1795).

Deliberately a fan-out rather than a parallel implementation: the per-spool
update carries the filament re-linking, extra-dict and shared-filament rules,
and a second copy of that logic here would drift from it silently.

What needs pinning is the failure behaviour. Spoolman is a REMOTE service, so a
single 404 (someone deleted that spool) or a transient connection error must not
throw away the work already done for the other rows — which is exactly what an
un-caught exception mid-loop would do, along with skipping the refresh
broadcast, leaving the table showing a partial state it never learns about.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from backend.app.api.routes.spoolman_inventory import _bulk_fanout


class TestBulkFanout:
    @pytest.mark.asyncio
    async def test_all_succeed(self):
        seen: list[int] = []

        async def action(spool_id: int):
            seen.append(spool_id)

        result = await _bulk_fanout([1, 2, 3], action)

        assert seen == [1, 2, 3]
        assert result == {"succeeded": 3, "errors": []}

    @pytest.mark.asyncio
    async def test_one_http_error_does_not_abort_the_batch(self):
        async def action(spool_id: int):
            if spool_id == 2:
                raise HTTPException(status_code=404, detail="Spool not found")

        result = await _bulk_fanout([1, 2, 3], action)

        # 1 and 3 still went through — the user's other rows are not lost
        # because one id went stale between the click and the request.
        assert result["succeeded"] == 2
        assert result["errors"] == [{"id": 2, "status": 404, "detail": "Spool not found"}]

    @pytest.mark.asyncio
    async def test_a_non_http_exception_is_collected_too(self):
        """A transient httpx error is the realistic case and is NOT an
        HTTPException — catching only that would abort the whole route with a
        500 and lose the accumulated state."""

        async def action(spool_id: int):
            if spool_id == 2:
                raise ConnectionError("spoolman unreachable")

        result = await _bulk_fanout([1, 2, 3], action)

        assert result["succeeded"] == 2
        assert result["errors"][0]["id"] == 2
        assert result["errors"][0]["status"] == 500
        assert "unreachable" in result["errors"][0]["detail"]

    @pytest.mark.asyncio
    async def test_all_failed_is_distinguishable_from_all_succeeded(self):
        async def action(spool_id: int):
            raise HTTPException(status_code=409, detail="nope")

        result = await _bulk_fanout([1, 2], action)

        # succeeded == 0 with a populated errors list is what lets the UI show a
        # red toast and KEEP the selection, instead of a green "0 updated".
        assert result["succeeded"] == 0
        assert len(result["errors"]) == 2
