"""N62 §9.4 (r1 B03): one admission function for "may this author's job stand on this printer".

Every place that gathers the printers an Auto Queue job may go to asks
``core/printer_scope.author_scope``: the distributor (``printers_for_item`` — the router, assign-now
and the retries all go through it), the rebalance's own copy of the gates (``_load_forecast``:
``allowed`` and the line work it may move) and the forecast's staged jobs (``_staged``). A new
gatherer that skips it would place a team's job on another team's printer; this names them.
"""

from __future__ import annotations

import inspect

from backend.app.services import auto_queue_eligibility, farm_forecast, queue_rebalance

GATHERERS = (
    auto_queue_eligibility.printers_for_item,
    queue_rebalance._load_forecast,
    farm_forecast._staged,
)


def test_every_gatherer_asks_the_author():
    silent = [f.__qualname__ for f in GATHERERS if "author_scope(" not in inspect.getsource(f)]
    assert not silent, silent


def test_the_rebalance_gates_carry_the_author_into_the_oracle():
    source = inspect.getsource(queue_rebalance._load_forecast)
    # the tail's gates and the printer-queue line work it may move
    assert "author_scopes.get(row.created_by_id)" in source
    assert "author_allowed(row)" in source


def test_a_staged_job_carries_its_authors_printers():
    assert "printers" in farm_forecast.StagedJob.__dataclass_fields__
