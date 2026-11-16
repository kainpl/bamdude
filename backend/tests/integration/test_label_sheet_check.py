"""What is wrong with a sheet being typed, asked of the server before Save.

The sheet editor used to learn about a grid that runs off its paper only by
pressing Save (a 422 toast) or by rendering the whole page. The arithmetic is
the server's — the paper table, the half-millimetre slack, the bounds — and a
second copy of it in the browser would drift. So the editor asks, debounced,
and this door answers every problem at once, in the system's language, without
drawing or saving anything.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

CHECK = "/api/v1/label-templates/sheets/check"
SHEETS = "/api/v1/label-templates/sheets"

A_SHEET = {
    "name": "My stock",
    "page_size": "A4",
    "cell_width_mm": 63.5,
    "cell_height_mm": 38.1,
    "cols": 3,
    "rows": 7,
    "margin_top_mm": 15.0,
    "margin_left_mm": 7.0,
    "gap_x_mm": 2.5,
    "gap_y_mm": 0.0,
}


async def _problems(async_client: AsyncClient, **change) -> list[str]:
    answered = await async_client.post(CHECK, json={**A_SHEET, **change})
    assert answered.status_code == 200, answered.text
    return answered.json()["problems"]


async def test_a_sheet_that_fits_has_nothing_to_say(async_client: AsyncClient):
    assert await _problems(async_client) == []


async def test_both_axes_are_named_at_once(async_client: AsyncClient):
    problems = await _problems(async_client, cols=4, rows=9)

    assert len(problems) == 2
    assert "wide" in problems[0] and "height" in problems[1]


async def test_it_says_what_save_would_refuse(async_client: AsyncClient):
    """The same sentence, from the same function — never a second opinion."""
    problems = await _problems(async_client, cols=4)
    refused = await async_client.post(SHEETS, json={**A_SHEET, "cols": 4})

    assert refused.status_code == 422
    assert problems == [refused.json()["detail"]]


@pytest.mark.parametrize(
    "change",
    [{"cols": 0}, {"rows": 0}, {"cell_width_mm": 0}, {"margin_left_mm": -1}, {"gap_y_mm": -0.5}],
)
async def test_a_half_typed_number_is_a_sentence_not_a_refusal(async_client: AsyncClient, change):
    """Clearing a field to type another passes through zero. That is a moment
    of editing, and it gets words rather than a validation error."""
    problems = await _problems(async_client, **change)

    assert len(problems) == 1


async def test_the_bounds_are_reported_too(async_client: AsyncClient):
    assert await _problems(async_client, cell_width_mm=4.0) != []


async def test_an_unknown_paper_is_a_sentence(async_client: AsyncClient):
    assert len(await _problems(async_client, page_size="A3")) == 1


async def test_nothing_is_saved(async_client: AsyncClient):
    await _problems(async_client)

    assert (await async_client.get(SHEETS)).json() == []


async def test_the_sentences_come_in_the_system_s_language(async_client: AsyncClient, monkeypatch):
    monkeypatch.setattr("backend.app.i18n._current_language", "uk")

    problems = await _problems(async_client, cols=4)

    assert "Сітка завширшки" in problems[0]
