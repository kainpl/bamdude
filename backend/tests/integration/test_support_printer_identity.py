"""One anonymous index names one machine across a support artifact (spec gh109-o2 r2).

The printers section, the per-printer queue rows, the connection diagnostics and the ZIP's push-status files each
read their own list of printers -- unordered, ordered by id, active-only -- so index 2 in one section was not always
index 2 in another, and diagnostics carried no index at all. Every section now reads one roster captured once per
artifact, with the printer states taken alongside it; a printer deleted, re-added or added during the collection
never shifts or borrows another's index. The real collector and ZIP builder run here against the test database;
every network leaf (port check, connection probe) is a stub, and nothing may leave loopback.
"""

from __future__ import annotations

import asyncio
import copy
import io
import json
import zipfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import delete, event, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.models.printer import Printer
from backend.app.models.printer_queue import PrinterQueue
from backend.app.models.settings import Settings
from backend.app.services.printer_manager import printer_manager

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    import ipaddress
    import socket

    def check(address):
        host = address[0] if isinstance(address, tuple) else address
        try:
            if ipaddress.ip_address(host).is_loopback:
                return
        except ValueError:
            if host == "localhost":
                return
        raise AssertionError(f"a support test tried to reach {address!r}")

    real_connect, real_create = socket.socket.connect, socket.create_connection

    def connect(sock, address):
        check(address)
        return real_connect(sock, address)

    def create_connection(address, *args, **kwargs):
        check(address)
        return real_create(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket, "create_connection", create_connection)


class _Client:
    def __init__(self, marker: str):
        self.state = SimpleNamespace(
            connected=True,
            state="IDLE",
            firmware_version="01.00",
            wifi_signal="-40dBm",
            hms_errors=[],
            developer_mode=False,
            nozzle_rack=[],
            raw_data={"marker": marker, "ams": []},
        )

    def check_staleness(self):
        pass


@pytest.fixture
def support_env(monkeypatch, test_engine):
    from backend.app.api.routes import support

    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    monkeypatch.setattr(support, "async_session", factory)
    monkeypatch.setattr("backend.app.core.database.async_session", factory)
    monkeypatch.setattr(support, "_check_port", AsyncMock(return_value=False))
    monkeypatch.setattr(support, "_get_log_content", lambda **_k: b"")
    probes = {}

    async def probe(ip_address, *, printer=None, **_k):
        hook = probes.get("hook")
        if hook is not None:
            await hook(printer)
        if probes.get("fail") == printer.id:
            raise RuntimeError("probe broke")
        return {"reachable": False}

    monkeypatch.setattr("backend.app.services.printer_diagnostic.run_connection_diagnostic", probe)
    monkeypatch.setattr("backend.app.services.log_health.scan_logs", lambda: {})
    return SimpleNamespace(support=support, factory=factory, probes=probes)


async def _farm(db, printer_factory, monkeypatch, *, with_holes=True):
    """Printers by id with a hole, distinct models and queue states, and a live client each (except one)."""
    made = []
    for n, (model, queue_status) in enumerate(
        [("X1C", "printing"), ("P1S", "idle"), ("A1", "paused"), ("H2D", "error")], start=1
    ):
        printer = await printer_factory(model=model, ip_address=f"192.0.2.{10 + n}", name=f"canary-name-{n}")
        made.append(SimpleNamespace(id=printer.id, model=model, queue_status=queue_status))
    if with_holes:
        await db.execute(delete(Printer).where(Printer.id == made[1].id))  # a hole in the ids
        await db.commit()
        made.pop(1)
    for entry in made:
        db.add(PrinterQueue(id=entry.id, printer_id=entry.id, status=entry.queue_status))
    await db.commit()
    for entry in made[:-1]:  # the last one has never been heard from
        monkeypatch.setitem(printer_manager._clients, entry.id, _Client(f"state-of-{entry.model}"))
    return made


def _by_model(info):
    return {entry["model"]: entry["index"] for entry in info["printers"]}


def _assert_one_machine_per_index(info, farm, zip_files=None):
    index = _by_model(info)
    assert set(index) == {p.model for p in farm}
    queue = {row["index"]: row["queue_status"] for row in info["queue"]["per_printer"]}
    diagnostics = {row["printer_id"]: row["index"] for row in info["diagnostics"]["connection_diagnostics"]}
    for p in farm:
        assert queue[index[p.model]] == p.queue_status, p
        assert diagnostics[p.id] == index[p.model], p
        if zip_files is not None and p is not farm[-1]:
            pushed = json.loads(zip_files[f"push-status/printer-{index[p.model]}.json"])
            assert pushed["model"] == p.model and pushed["index"] == index[p.model]
            assert pushed["raw_data"]["marker"] == f"state-of-{p.model}"


async def _zip(support_env, mqtt_printer_ids=""):
    async with support_env.factory() as db:
        db.add(Settings(key="debug_logging_enabled", value="true"))
        await db.commit()
    response = await support_env.support.generate_support_bundle(
        request=None, mqtt_printer_ids=mqtt_printer_ids, _=None, creds=None
    )
    body = b"".join([chunk async for chunk in response.body_iterator])
    with zipfile.ZipFile(io.BytesIO(body)) as zf:
        files = {name: zf.read(name) for name in zf.namelist()}
    return json.loads(files["support-info.json"]), files, body


# --------------------------------------------------------------------------- #
# A01 / A02 / A11 -- every section, one index per machine
# --------------------------------------------------------------------------- #


async def test_every_section_and_file_names_one_machine_by_one_index(
    db_session, printer_factory, support_env, monkeypatch
):
    farm = await _farm(db_session, printer_factory, monkeypatch)

    info, files, _ = await _zip(support_env)

    _assert_one_machine_per_index(info, farm, files)
    assert info["printer_index"]["version"] == 1  # the artifact says what its indices mean
    assert sorted(_by_model(info).values()) == [1, 2, 3]
    # The legacy diagnostic id stays the printer's id (A11): the raw MQTT file name carries it too.
    assert {row["printer_id"] for row in info["diagnostics"]["connection_diagnostics"]} == {p.id for p in farm}


async def test_inactive_archived_and_queueless_printers_shift_nobody(
    db_session, printer_factory, support_env, monkeypatch
):
    farm = await _farm(db_session, printer_factory, monkeypatch, with_holes=False)
    await db_session.execute(update(Printer).where(Printer.id == farm[0].id).values(is_active=False))
    await db_session.execute(update(Printer).where(Printer.id == farm[1].id).values(archived=True))
    await db_session.execute(delete(PrinterQueue).where(PrinterQueue.printer_id == farm[2].id))
    await db_session.commit()

    info, files, _ = await _zip(support_env)

    index = _by_model(info)
    assert sorted(index.values()) == [1, 2, 3, 4]
    diagnosed = {row["printer_id"]: row["index"] for row in info["diagnostics"]["connection_diagnostics"]}
    assert set(diagnosed) == {farm[2].id, farm[3].id}  # active, not archived -- with their roster index, gaps fine
    assert diagnosed[farm[3].id] == index["H2D"]
    queue_indices = {row["index"] for row in info["queue"]["per_printer"]}
    assert index[farm[2].model] not in queue_indices  # no invented idle queue
    assert f"push-status/printer-{index['X1C']}.json" in files


# --------------------------------------------------------------------------- #
# A03 / A04 / A09 -- membership changes during the collection; a probe that breaks
# --------------------------------------------------------------------------- #


async def test_a_printer_deleted_and_one_added_during_the_collection_shift_nothing(
    db_session, printer_factory, support_env, monkeypatch
):
    farm = await _farm(db_session, printer_factory, monkeypatch, with_holes=False)
    gone, late = farm[0], {}

    async def during_the_probes(_printer):
        if "done" in late:
            return
        late["done"] = True
        async with support_env.factory() as db:
            await db.execute(delete(PrinterQueue).where(PrinterQueue.printer_id == gone.id))
            await db.execute(delete(Printer).where(Printer.id == gone.id))
            db.add(Printer(name="late", serial_number="LATE1", ip_address="192.0.2.99", access_code="x", model="A1M"))
            await db.commit()

    support_env.probes["hook"] = during_the_probes

    info, files, _ = await _zip(support_env)

    assert "A1M" not in _by_model(info)
    _assert_one_machine_per_index(info, farm, files)


async def test_a_reused_id_never_lends_its_new_state_to_the_old_index(
    db_session, printer_factory, support_env, monkeypatch
):
    """Deleted and re-added under the same id and serial after the capture: the captured state stays."""
    farm = await _farm(db_session, printer_factory, monkeypatch, with_holes=False)
    reused = farm[0]

    async def during_the_probes(_printer):
        monkeypatch.setitem(printer_manager._clients, reused.id, _Client("state-of-the-NEW-printer"))

    support_env.probes["hook"] = during_the_probes

    info, files, _ = await _zip(support_env)

    pushed = json.loads(files[f"push-status/printer-{_by_model(info)[reused.model]}.json"])
    assert pushed["raw_data"]["marker"] == f"state-of-{reused.model}"


async def test_a_probe_that_breaks_moves_no_other_index(db_session, printer_factory, support_env, monkeypatch):
    farm = await _farm(db_session, printer_factory, monkeypatch)
    support_env.probes["fail"] = farm[1].id

    info, files, _ = await _zip(support_env)

    _assert_one_machine_per_index(info, farm, files)
    broken = [r for r in info["diagnostics"]["connection_diagnostics"] if r["printer_id"] == farm[1].id]
    assert broken and "error" in broken[0]


async def test_two_bundles_keep_their_own_rosters(db_session, printer_factory, support_env, monkeypatch):
    farm = await _farm(db_session, printer_factory, monkeypatch)
    first, second = await asyncio.gather(
        support_env.support._collect_support_info(), support_env.support._collect_support_info()
    )
    for info in (first, second):
        _assert_one_machine_per_index(info, farm)

    # The next artifact after a printer left is its own: nothing of the earlier roster is reused.
    await db_session.execute(delete(PrinterQueue).where(PrinterQueue.printer_id == farm[0].id))
    await db_session.execute(delete(Printer).where(Printer.id == farm[0].id))
    await db_session.commit()
    third = await support_env.support._collect_support_info()
    assert farm[0].model not in _by_model(third)
    _assert_one_machine_per_index(third, farm[1:])


@pytest.mark.parametrize("scope", [None, frozenset(), "subset"], ids=["all", "none", "subset"])
async def test_the_print_state_scope_reaches_its_section_unchanged_and_moves_no_index(
    db_session, printer_factory, support_env, monkeypatch, scope
):
    """A05: the scope narrows the print-state summary only -- None and empty are different -- and indices stay."""
    from backend.app.services.print_state_observer import print_state_observer

    farm = await _farm(db_session, printer_factory, monkeypatch)
    if scope == "subset":
        scope = frozenset({farm[1].id})
    asked = []
    monkeypatch.setattr(print_state_observer, "public_summary", lambda s: asked.append(s) or {"by_code": {}})

    info = await support_env.support._collect_support_info(print_state_scope=scope, include_print_state=True)

    assert asked == [scope]
    _assert_one_machine_per_index(info, farm)


# --------------------------------------------------------------------------- #
# A07 -- the issue projection trims a copy, never the caller's snapshot
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("room", ["whole-then-measured", "trimmed-in-the-first-pass"])
async def test_the_issue_projection_leaves_the_snapshot_whole(room):
    """Both trims: a list kept whole and then shortened by the measured pass, and a list trimmed in the first pass."""
    from backend.app.services.support_projection import _size, project_for_issue

    # Indices with gaps, as a roster has them: a renumbering would show.
    printers = [{"index": 2 * i + 1, "model": "X1C", "nested": {"slots": [1, 2, 3]}} for i in range(200)]
    info = {"printers": printers, "queue": {"per_printer": [{"index": 2 * i + 1} for i in range(200)]}}
    budget = _size(printers) + len("printers") + 8 + 2
    if room == "trimmed-in-the-first-pass":
        budget //= 2
    before = copy.deepcopy(info)

    first, notes = project_for_issue(info, budget_chars=budget)
    assert notes  # the case trims for real
    assert info == before  # nested lists and dicts included
    second, _notes = project_for_issue(info, budget_chars=budget)
    assert first == second
    kept = [p["index"] for p in first["printers"]]
    assert kept and kept == [p["index"] for p in printers[: len(kept)]]  # the trimmed list keeps its own indices


# --------------------------------------------------------------------------- #
# A08 -- nothing private leaves; raw MQTT keeps its opt-in, its name and gets the shared index
# --------------------------------------------------------------------------- #


async def test_raw_mqtt_follows_the_index_and_nothing_private_leaves(
    db_session, printer_factory, support_env, monkeypatch, tmp_path
):
    from backend.app.services.mqtt_recorder import mqtt_recorder

    farm = await _farm(db_session, printer_factory, monkeypatch)
    outside = 9_999  # a recording of a printer the roster does not have
    recordings = {}
    for pid in (farm[0].id, outside):
        path = tmp_path / f"mqtt-20261104-{pid}.log"
        path.write_text("raw capture", encoding="utf-8")
        recordings[pid] = path
    monkeypatch.setattr(mqtt_recorder, "file_for", lambda pid: recordings.get(pid))

    info, files, body = await _zip(support_env, mqtt_printer_ids=f"{farm[0].id},{outside}")

    index = _by_model(info)[farm[0].model]
    assert f"mqtt/printer-{index}/{recordings[farm[0].id].name}" in files
    assert f"mqtt/{recordings[outside].name}" in files  # kept, under its own raw name
    # Every file as it unpacks -- the ZIP's own bytes are deflated and would hide anything. The raw captures are the
    # one explicit, unsanitised opt-in and are left out of this check.
    assert body
    for name, data in files.items():
        if name.startswith("mqtt/"):
            continue
        for secret in ("canary-name-1", "192.0.2.11", "00M09A"):
            assert secret.encode() not in data, (name, secret)


# --------------------------------------------------------------------------- #
# A10 -- the roster costs the same few statements for any farm size
# --------------------------------------------------------------------------- #


async def test_the_roster_costs_a_fixed_number_of_statements(db_session, printer_factory, test_engine, support_env):
    async def statements_for(count):
        for n in range(count):
            p = await printer_factory(model=f"M{n}", ip_address=f"192.0.2.{120 + n}")
            db_session.add(PrinterQueue(id=p.id, printer_id=p.id, status="idle"))
        await db_session.commit()
        seen = []

        def record(*_args):
            seen.append(1)

        event.listen(test_engine.sync_engine, "before_cursor_execute", record)
        try:
            await support_env.support._capture_support_roster()
        finally:
            event.remove(test_engine.sync_engine, "before_cursor_execute", record)
        return len(seen)

    small = await statements_for(2)
    large = await statements_for(6)
    assert small == large
