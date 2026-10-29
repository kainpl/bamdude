"""The process's collector policy decides once, from what it is handed (spec process-gc-latency r2).

The real collector is never touched here: the shared pytest worker runs with the policy off
(conftest), and freezing it would be a test-only fix nobody runs in production. Each test hands a
fresh ``_Boundary`` a fake collector and the facts it decides from; the real start-up — the
lifespan calling it first, once, before the database — is proven in a process of its own
(``integration/test_process_gc_startup.py``).
"""

import logging
from types import SimpleNamespace

import pytest

from backend.app.core import process_gc


class FakeCollector:
    def __init__(self, *, enabled=True, frozen=0):
        self.enabled = enabled
        self.frozen = frozen
        self.calls = []

    def isenabled(self):
        return self.enabled

    def get_freeze_count(self):
        return self.frozen

    def collect(self, generation=2):
        self.calls.append(("collect", generation))
        return 0

    def freeze(self):
        self.calls.append(("freeze",))
        self.frozen += 1000


CPYTHON_312 = SimpleNamespace(implementation="cpython", version=(3, 12, 10), free_threaded=False)


def _boundary(collector=None, *, interpreter=CPYTHON_312, working=None):
    return process_gc._Boundary(
        collector or FakeCollector(), interpreter=lambda: interpreter, already_working=lambda: working
    )


def test_on_by_default_it_collects_then_freezes_once():
    collector = FakeCollector()
    boundary = _boundary(collector)
    first = boundary.prepare({})
    assert collector.calls == [("collect", 2), ("freeze",)]
    assert (first.state, first.reason, first.frozen) == ("enabled", None, 1000)
    assert boundary.prepare({}) is first
    assert collector.calls == [("collect", 2), ("freeze",)]


@pytest.mark.parametrize("value", ["on", "ON", " on ", "true", "1", ""])
def test_every_spelling_of_on_freezes(value):
    collector = FakeCollector()
    assert _boundary(collector).prepare({process_gc.ENV: value}).state == "enabled"
    assert ("freeze",) in collector.calls


@pytest.mark.parametrize("value", ["off", "OFF", " off", "false", "0"])
def test_off_leaves_the_collector_as_it_is(value):
    collector = FakeCollector()
    outcome = _boundary(collector).prepare({process_gc.ENV: value})
    assert (outcome.state, outcome.reason, outcome.frozen) == ("skipped", "disabled_by_env", 0)
    assert collector.calls == []


@pytest.mark.parametrize("value", ["maybe", "yes please", "2", "offf"])
def test_an_unknown_value_refuses_the_start_and_decides_nothing(value):
    collector = FakeCollector()
    boundary = _boundary(collector)
    with pytest.raises(process_gc.GcPolicyConfigError, match=process_gc.ENV):
        boundary.prepare({process_gc.ENV: value})
    assert collector.calls == []
    # The refusal is no attempt: a corrected start still decides.
    assert boundary.prepare({}).state == "enabled"


def test_a_host_that_switched_the_collector_off_is_left_alone():
    collector = FakeCollector(enabled=False)
    outcome = _boundary(collector).prepare({})
    assert (outcome.state, outcome.reason) == ("skipped", "gc_disabled")
    assert collector.calls == []


@pytest.mark.parametrize("change", [+5, -100])
def test_a_host_that_froze_or_unfroze_after_the_import_is_left_alone(change):
    collector = FakeCollector(frozen=377)
    boundary = _boundary(collector)
    collector.frozen += change
    outcome = boundary.prepare({})
    assert (outcome.state, outcome.reason, outcome.frozen) == ("skipped", "already_frozen", 377 + change)
    assert collector.calls == []


def test_the_interpreter_s_own_startup_set_is_no_host_s():
    """CPython 3.12 starts with 377 frozen tuples (measured); that is the count at import."""
    collector = FakeCollector(frozen=377)
    outcome = _boundary(collector).prepare({})
    assert (outcome.state, outcome.frozen) == ("enabled", 1377)


@pytest.mark.parametrize(
    ("interpreter", "reason"),
    [
        (SimpleNamespace(implementation="cpython", version=(3, 12, 10), free_threaded=True), "free_threaded"),
        (SimpleNamespace(implementation="pypy", version=(3, 12, 1), free_threaded=False), "unverified_interpreter"),
        (SimpleNamespace(implementation="cpython", version=(3, 11, 9), free_threaded=False), "unverified_interpreter"),
    ],
)
def test_an_interpreter_outside_the_measured_cells_is_left_alone(interpreter, reason):
    collector = FakeCollector()
    outcome = _boundary(collector, interpreter=interpreter).prepare({})
    assert (outcome.state, outcome.reason) == ("skipped", reason)
    assert collector.calls == []


def test_a_process_that_already_works_is_never_frozen():
    collector = FakeCollector()
    outcome = _boundary(collector, working="database_open").prepare({})
    assert (outcome.state, outcome.reason) == ("skipped", "late_start")
    assert collector.calls == []


def test_a_skipped_attempt_is_the_process_s_one_attempt():
    """A first start that kept the stock collector never freezes on a later lifespan: by then the
    process has sessions, tasks and clients the boundary must never take."""
    collector = FakeCollector()
    boundary = _boundary(collector)
    assert boundary.prepare({process_gc.ENV: "off"}).reason == "disabled_by_env"
    assert boundary.prepare({}).reason == "disabled_by_env"
    assert collector.calls == []


def test_one_record_per_process(caplog):
    boundary = _boundary()
    with caplog.at_level(logging.INFO, logger=process_gc.logger.name):
        boundary.prepare({})
        boundary.prepare({})
    records = [r for r in caplog.records if r.name == process_gc.logger.name]
    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert "enabled" in message and "3.12.10" in message and "1000" in message


@pytest.mark.parametrize(
    ("enabled", "froze_since", "env", "level"),
    [
        (False, 0, {}, logging.WARNING),
        (True, 1, {}, logging.WARNING),
        (True, 0, {process_gc.ENV: "off"}, logging.INFO),
    ],
)
def test_a_skip_says_why_in_one_record(caplog, enabled, froze_since, env, level):
    collector = FakeCollector(enabled=enabled)
    boundary = _boundary(collector)
    collector.frozen += froze_since
    with caplog.at_level(logging.INFO, logger=process_gc.logger.name):
        outcome = boundary.prepare(env)
    records = [r for r in caplog.records if r.name == process_gc.logger.name]
    assert [r.levelno for r in records] == [level]
    assert outcome.reason in records[0].getMessage()


def test_the_interpreter_facts_are_this_one():
    facts = process_gc._interpreter()
    import sys

    assert facts.version == tuple(sys.version_info[:3])
    assert facts.implementation == sys.implementation.name


def _fake_database(checkedin=0, checkedout=0):
    pool = SimpleNamespace(checkedin=lambda: checkedin, checkedout=lambda: checkedout)
    return SimpleNamespace(engine=SimpleNamespace(sync_engine=SimpleNamespace(pool=pool)))


def _fake_printers(clients):
    return SimpleNamespace(printer_manager=SimpleNamespace(_clients=clients))


@pytest.mark.parametrize(
    ("database", "printers", "expected"),
    [
        (None, None, None),
        (_fake_database(), _fake_printers({}), None),
        (_fake_database(checkedin=1), _fake_printers({}), "database_open"),
        (_fake_database(checkedout=1), None, "database_open"),
        (_fake_database(), _fake_printers({7: object()}), "printer_clients"),
    ],
)
def test_the_working_probe_reads_the_engine_pool_and_the_printer_clients(monkeypatch, database, printers, expected):
    """Read through ``sys.modules`` only: a module nobody imported has started nothing, and the
    probe never imports one to ask."""
    import sys

    for name, fake in (
        ("backend.app.core.database", database),
        ("backend.app.services.printer_manager", printers),
    ):
        if fake is None:
            monkeypatch.delitem(sys.modules, name, raising=False)
        else:
            monkeypatch.setitem(sys.modules, name, fake)
    assert process_gc._already_working() == expected
