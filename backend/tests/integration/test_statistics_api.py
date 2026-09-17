"""The statistics API has its own home: ``/api/v1/statistics/…``.

Bodies moved verbatim out of ``routes/archives.py`` (vault
60-specs/statistics-module-spec); what these pin is the contract of the move -
the five new paths answer, the five old ones are gone (not aliased), and the
``/statistics/reports`` sub-path is reserved with nothing under it yet.

⚠️ "Gone" is checked against OpenAPI, not against a status code: a one-segment
old path such as ``/archives/stats`` falls through to the ``/archives/{archive_id}``
catch-all and answers 422, not 404 (see ``test_the_slim_endpoint_is_gone``).
"""

import pytest

from backend.app.main import app

NEW = [
    ("get", "/api/v1/statistics/overview"),
    ("get", "/api/v1/statistics/aggregate"),
    ("get", "/api/v1/statistics/failures"),
    ("get", "/api/v1/statistics/export"),
    ("post", "/api/v1/statistics/recalculate-costs"),
]
OLD = [
    "/api/v1/archives/stats",
    "/api/v1/archives/aggregate",
    "/api/v1/archives/analysis/failures",
    "/api/v1/archives/stats/export",
    "/api/v1/archives/recalculate-costs",
]


def test_the_five_paths_moved_and_nothing_answers_at_the_old_ones():
    paths = app.openapi()["paths"]
    for method, path in NEW:
        assert path in paths and method in paths[path], (method, path)
    for path in OLD:
        assert path not in paths, path
    # Reserved, not built: the sub-router exists so the path has an owner.
    assert not [p for p in paths if p.startswith("/api/v1/statistics/reports")]


@pytest.mark.asyncio
async def test_the_new_paths_answer_over_http(async_client):
    for method, path in NEW:
        response = await async_client.request(method.upper(), path)
        assert response.status_code == 200, (method, path, response.status_code, response.text[:200])
