"""Archive search folds Cyrillic case on SQLite — the default backend.

SQLAlchemy compiles ``ilike`` to ``lower(col) LIKE lower(:q)``, and SQLite's
built-in ``lower()`` knows ASCII only: ``?search=КРОНШТЕЙН`` found nothing while
«Кронштейн» sat in the table, and so did ``?search=кронштейн``. The fix
(``core/case_folding.py``) shadows ``lower``/``upper`` with Python's on every
connection; ``tests/conftest.py`` attaches the same ``connect`` listener as
production, so these requests go through the real fold rather than a stub of it.

⚠️ The ``/archives/search`` route below cannot reach its FTS5 index under test:
the test engine is built by ``create_all`` alone and ``archive_fts`` is a
virtual table only m001 creates, so the MATCH statement fails, its SAVEPOINT is
rolled back and the ``ilike`` fallback answers. That fallback is what this file
pins — in production SQLite the FTS5 ``unicode61`` tokenizer folds case itself,
which is why the FTS branch was never the bug. It also means the request
survives a refused index query at all, on a real session rather than a fake one.
"""

import pytest
from httpx import AsyncClient


@pytest.fixture
async def cyrillic_archive(archive_factory, printer_factory):
    # A name that shares no token with the search: were the route ever widened
    # to printer names, a printer called «Кронштейн-принтер» would keep these
    # tests green for the wrong reason.
    printer = await printer_factory(name="Принтер 1")
    return await archive_factory(printer.id, print_name="Кронштейн", filename="kronshtein.gcode.3mf")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_upper_case_finds_the_mixed_case_row(async_client: AsyncClient, cyrillic_archive):
    response = await async_client.get("/api/v1/archives/?search=КРОНШТЕЙН")

    assert response.status_code == 200
    names = [a["print_name"] for a in response.json()["data"]]
    assert names == ["Кронштейн"], names


@pytest.mark.asyncio
@pytest.mark.integration
async def test_lower_case_finds_the_capitalised_row(async_client: AsyncClient, cyrillic_archive):
    """The other direction, and the one an operator actually types: the query is
    folded as well as the column, so neither side may be the ASCII-only one."""
    response = await async_client.get("/api/v1/archives/?search=кронштейн")

    assert response.status_code == 200
    names = [a["print_name"] for a in response.json()["data"]]
    assert names == ["Кронштейн"], names


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_word_that_is_not_there_still_finds_nothing(async_client: AsyncClient, cyrillic_archive):
    """The guard that makes the two tests above mean something: a fold that
    matched everything would pass them both."""
    response = await async_client.get("/api/v1/archives/?search=КРАН")

    assert response.status_code == 200
    assert response.json()["data"] == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_full_text_route_falls_back_and_still_folds(async_client: AsyncClient, cyrillic_archive):
    """``GET /archives/search`` with no ``archive_fts`` to ask — see the module
    docstring. The refused statement must not take the request with it, and the
    fallback must fold."""
    response = await async_client.get("/api/v1/archives/search?q=КРОНШТЕЙН")

    assert response.status_code == 200
    names = [a["print_name"] for a in response.json()]
    assert names == ["Кронштейн"], names
