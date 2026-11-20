"""Every writer of a queue row has a lock class, and its code keeps it (G03 B, A13).

The scanner (``backend/tests/queue_writers/writer_inventory.py``, the r5 evidence's) reports every function of
``backend/app`` that writes, locks or deletes ``print_queue``, ``printer_queues`` or ``auto_queue_items`` rows,
directly or through a writer helper. ``classification.json`` beside it names each function's lock class; the
rules are ``backend/tests/queue_writers/guard.py``. The test fails on a function with a site and no entry, on an
entry that names no function, and on any rule below. Each rule has a negative control over a small synthetic
``backend/app`` tree, so a rule that stopped detecting is a red test too.

Rules switched on now: the inventory itself; the lane cover of every ``lane`` function (lexically inside a lane
scope, not after a commit inside it, or an exemption read in the code); untyped sites read one by one; a lane-less
writer never writes a column that decides a queue's next row, nor builds a job row (A15); every function that sets
a foreign key of a queue table says how the parent is held (L6, A24); every explicit lock of a queue row, and the
order row ``policy_for`` takes, is FOR NO KEY UPDATE (L6(b)). The swap refresh's row locks go through a model a
function returns, which the scan resolves only with r6 rule (4) (A31); they are NO KEY UPDATE already. The rules of
A9, A16, A20, A25 and r6 (4)–(7) are switched on by the change that makes their code change.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import asdict

import pytest

from backend.tests.queue_writers import guard, writer_inventory as wi

pytestmark = pytest.mark.timeout(600)


@pytest.fixture(scope="module")
def tree():
    return guard.scan()


@pytest.fixture(scope="module")
def data():
    return guard.classification()


# ---------------------------------------------------------------- the real tree


def test_every_function_with_a_site_has_an_entry_and_every_entry_a_function(tree, data):
    unclassified, stale = guard.unclassified_and_stale(tree, data)
    assert unclassified == [], "classify these in classification.json (lock class + rule)"
    assert stale == [], "these entries name no function the scan reports any more"


def test_every_entry_names_a_lock_class_of_the_code_as_it_is(data):
    assert guard.bad_locks(data) == []


def test_every_lane_function_writes_inside_its_lane(tree, data):
    assert guard.lane_violations(tree, data) == []


def test_every_exemption_still_names_a_site(tree, data):
    assert guard.stale_exemptions(tree, data) == []


def test_every_untyped_site_has_been_read(tree, data):
    assert guard.unread_candidates(tree, data) == []
    assert guard.confirmed_writers_mismatch(tree, data) == []


def test_a_lane_less_writer_never_moves_a_queues_next_row(tree, data):
    assert guard.head_writes_without_a_lane(tree, data) == []
    assert guard.job_rows_built_without_a_lane(tree, data) == []


def test_every_foreign_key_a_writer_sets_says_how_its_parent_is_held(tree, data):
    assert guard.fk_unrecorded(tree, data) == []


def test_every_explicit_lock_of_a_queue_row_lets_a_foreign_key_check_pass(tree):
    """L6(b): FOR NO KEY UPDATE, never FOR UPDATE — the order row ``policy_for`` takes included."""
    assert guard.plain_row_locks(tree) == []
    assert guard.order_lock_is_no_key_update() == []


@pytest.mark.parametrize("case", wi.SELFTEST_CASES, ids=lambda case: case[0][:60])
def test_a_raw_statement_is_attributed_to_the_table_its_verb_names(case):
    sql, dml, lock = case
    assert sorted(wi.dml_targets(sql)) == sorted(dml)
    assert sorted(wi.raw_lock_targets(sql)) == sorted(lock)


def test_two_orders_of_the_target_set_give_the_same_sites(tree, data, monkeypatch):
    def normal(sites):
        return sorted(json.dumps({k: v for k, v in asdict(s).items() if k != "id"}, sort_keys=True) for s in sites)

    monkeypatch.setattr(wi, "TARGETS", dict(reversed(list(wi.TARGETS.items()))))
    monkeypatch.setattr(wi, "TARGET_TABLES", set(wi.TARGETS.values()))
    again = guard.scan_of(guard.APP, data)
    assert normal(again.sites) == normal(tree.sites)


# ---------------------------------------------------------------- negative controls

MODELS = {
    "print_queue.py": """
        from sqlalchemy import ForeignKey, String
        from sqlalchemy.orm import Mapped, mapped_column

        from backend.app.core.database import Base


        class PrintQueueItem(Base):
            __tablename__ = "print_queue"
            id: Mapped[int] = mapped_column(primary_key=True)
            status: Mapped[str] = mapped_column(String(20))
            note: Mapped[str] = mapped_column(String(20))
            queue_id: Mapped[int] = mapped_column(ForeignKey("printer_queues.id"))
            archive_id: Mapped[int | None] = mapped_column(ForeignKey("print_archives.id"))
    """,
    "printer_queue.py": """
        from sqlalchemy import String
        from sqlalchemy.orm import Mapped, mapped_column

        from backend.app.core.database import Base


        class PrinterQueue(Base):
            __tablename__ = "printer_queues"
            id: Mapped[int] = mapped_column(primary_key=True)
            status: Mapped[str] = mapped_column(String(20))
    """,
    "auto_queue.py": """
        from sqlalchemy import String
        from sqlalchemy.orm import Mapped, mapped_column

        from backend.app.core.database import Base


        class AutoQueueItem(Base):
            __tablename__ = "auto_queue_items"
            id: Mapped[int] = mapped_column(primary_key=True)
            status: Mapped[str] = mapped_column(String(20))
    """,
}

DEMO = """
    from backend.app.models.print_queue import PrintQueueItem
    from backend.app.services.queue_ops import queue_claim_scope


    async def in_lane(db, queue_id):
        async with queue_claim_scope(db, queue_id):
            item = await db.get(PrintQueueItem, 1)
            item.note = "x"


    async def outside_lane(db):
        item = await db.get(PrintQueueItem, 1)
        item.note = "x"


    async def after_commit(db, queue_id):
        async with queue_claim_scope(db, queue_id):
            await db.commit()
            item = await db.get(PrintQueueItem, 1)
            item.note = "x"


    async def conditional(db, queue_id, held):
        from contextlib import nullcontext

        async with (nullcontext() if held else queue_claim_scope(db, queue_id)):
            item = await db.get(PrintQueueItem, 1)
            item.note = "x"


    async def stack_passed_in(db, guards, queue_id):
        await guards.enter_async_context(queue_claim_scope(db, queue_id))
        item = await db.get(PrintQueueItem, 1)
        item.note = "x"


    async def no_lane_helper(db):
        item = await db.get(PrintQueueItem, 1)
        item.note = "x"


    async def lane_through_a_helper(db):
        await no_lane_helper(db)


    async def lane_less_head_write(db):
        item = await db.get(PrintQueueItem, 1)
        item.status = "pending"


    async def lane_less_constructor(db):
        db.add(PrintQueueItem(note="x", queue_id=1))


    async def sets_a_parent(db):
        item = await db.get(PrintQueueItem, 1)
        item.archive_id = 7


    async def untyped(db, thing):
        thing.status = "cancelled"


    async def plain_lock(db):
        from sqlalchemy import select

        await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == 1).with_for_update())
        await db.get(PrintQueueItem, 1, with_for_update=True)


    async def no_key_lock(db):
        from sqlalchemy import select

        await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == 1).with_for_update(key_share=True))
        await db.get(PrintQueueItem, 1, with_for_update={"key_share": True})
        await db.execute(select(PrintQueueItem).where(PrintQueueItem.id == 1).with_for_update(key_share=False))
"""

QUEUE_OPS = """
    from contextlib import asynccontextmanager


    @asynccontextmanager
    async def queue_claim_scope(db, queue_id):
        yield
"""

D = "backend/app/services/demo.py::"


@pytest.fixture(scope="module")
def synthetic(tmp_path_factory):
    app = tmp_path_factory.mktemp("synthetic") / "backend" / "app"
    (app / "models").mkdir(parents=True)
    (app / "services").mkdir()
    for name, body in MODELS.items():
        (app / "models" / name).write_text(textwrap.dedent(body), encoding="utf-8")
    (app / "services" / "demo.py").write_text(textwrap.dedent(DEMO), encoding="utf-8")
    (app / "services" / "queue_ops.py").write_text(textwrap.dedent(QUEUE_OPS), encoding="utf-8")
    entries = {
        D + name: {"class": lock, "lock": lock}
        for name, lock in {
            "in_lane": "lane",
            "outside_lane": "lane",
            "after_commit": "lane",
            "conditional": "lane",
            "stack_passed_in": "lane",
            "no_lane_helper": "helper",
            "lane_through_a_helper": "lane",
            "lane_less_head_write": "L2b",
            "lane_less_constructor": "L2b",
            "sets_a_parent": "helper",
            "untyped": "n/a",
            "plain_lock": "helper",
            "no_key_lock": "helper",
        }.items()
    }
    return app, entries


def _check(synthetic, change=None):
    app, entries = synthetic
    data = json.loads(json.dumps(entries))
    if change:
        change(data)
    return guard.scan_of(app, data), data


def test_negative_control_an_unclassified_function_and_a_stale_entry(synthetic):
    def change(data):
        del data[D + "in_lane"]
        data[D + "gone"] = {"class": "lane", "lock": "lane"}

    scan_, data = _check(synthetic, change)
    assert guard.unclassified_and_stale(scan_, data) == ([D + "in_lane"], [D + "gone"])


def test_negative_control_a_lock_class_the_code_cannot_have(synthetic):
    def change(data):
        data[D + "in_lane"]["lock"] = "lane?"

    _scan, data = _check(synthetic, change)
    assert guard.bad_locks(data) == [D + "in_lane"]


def _function(finding: str) -> str:
    return finding.split("::", 1)[1].split(":", 1)[0]


def test_negative_control_the_lane_cover(synthetic):
    scan_, data = _check(synthetic)
    found = guard.lane_violations(scan_, data)
    assert {_function(v) for v in found} == {"outside_lane", "after_commit", "lane_through_a_helper"}, (
        "in_lane, the conditional scope and the stack passed in are inside"
    )
    assert all("follows a commit" in v for v in found if _function(v) == "after_commit")
    assert all("outside its lane" in v for v in found if _function(v) != "after_commit")


def test_negative_control_the_lane_exemptions(synthetic):
    def change(data):
        data[D + "outside_lane"]["outside"] = {"orm-attr:PrintQueueItem[note]": "read"}
        data[D + "after_commit"]["after_commit"] = {"orm-attr:PrintQueueItem[note]": "branch"}
        data[D + "lane_through_a_helper"]["via"] = ["demo.no_lane_helper"]

    scan_, data = _check(synthetic, change)
    found = guard.lane_violations(scan_, data)
    assert found == [D + "lane_through_a_helper: via demo.no_lane_helper, which enters no lane"]


def test_negative_control_a_stale_exemption(synthetic):
    def change(data):
        data[D + "in_lane"]["outside"] = {"orm-attr:PrintQueueItem[gone]": "read"}

    scan_, data = _check(synthetic, change)
    assert guard.stale_exemptions(scan_, data) == [D + "in_lane: outside orm-attr:PrintQueueItem[gone]"]


def test_negative_control_an_unread_untyped_site(synthetic):
    scan_, data = _check(synthetic)
    found = guard.unread_candidates(scan_, data)
    assert [_function(v) for v in found] == ["untyped"]
    read = found[0].split(" ", 1)[1]

    def change(data):
        data[D + "untyped"]["candidates"] = {read: "other: a test object"}

    scan_, data = _check(synthetic, change)
    assert guard.unread_candidates(scan_, data) == []


def test_negative_control_a_head_write_and_a_job_row_without_a_lane(synthetic):
    scan_, data = _check(synthetic)
    assert {_function(v) for v in guard.head_writes_without_a_lane(scan_, data)} == {
        "lane_less_head_write",
        "lane_less_constructor",  # its queue_id
    }
    assert [_function(v) for v in guard.job_rows_built_without_a_lane(scan_, data)] == ["lane_less_constructor"]


def test_negative_control_a_foreign_key_set_without_a_record(synthetic):
    scan_, data = _check(synthetic)
    found = guard.fk_unrecorded(scan_, data)
    assert D + "sets_a_parent: print_queue.archive_id" in found

    def change(data):
        for name in (D + "sets_a_parent", D + "lane_less_constructor"):
            data[name]["fk"] = {"print_queue.archive_id": "free", "print_queue.queue_id": "free"}

    scan_, data = _check(synthetic, change)
    assert guard.fk_unrecorded(scan_, data) == []


def test_negative_control_a_plain_row_lock(synthetic):
    scan_, _data = _check(synthetic)
    found = guard.plain_row_locks(scan_)
    assert sorted(_function(v) for v in found) == ["no_key_lock", "plain_lock", "plain_lock"], (
        "a plain FOR UPDATE, a db.get(with_for_update=True) and key_share=False are refused; the two NO KEY forms pass"
    )


def test_negative_control_a_plain_order_lock(tmp_path):
    services = tmp_path / "services"
    services.mkdir()
    source = """
        async def policy_for(db, project_id, *, lock=False):
            stmt = select(Project)
            if lock:
                stmt = stmt.with_for_update()
    """
    (services / "order_resources.py").write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    assert guard.order_lock_is_no_key_update(tmp_path) == ["policy_for:4 FOR UPDATE"]


def test_the_light_writers_lane_session_is_claim_lanes():
    """``queue_ops.lane_session`` counts as a lane scope for the scan only because it enters ``claim_lanes``."""
    import ast

    source = (guard.APP / "services" / "queue_ops.py").read_text(encoding="utf-8")
    fn = next(
        n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.AsyncFunctionDef) and n.name == "lane_session"
    )
    entered = [
        wi._call_name(item.context_expr)
        for node in ast.walk(fn)
        if isinstance(node, ast.AsyncWith)
        for item in node.items
        if isinstance(item.context_expr, ast.Call)
    ]
    assert "claim_lanes" in entered


def test_memory_functions_make_no_durable_write(tree, data):
    """A23: the eligibility's and the runner's back-fills are memory (``set_committed_value``), never a pending ORM
    change another writer's flush would carry; their entries still name the two readers."""
    assert guard.memory_functions_write_nothing(tree, data) == []
    kept = guard.memory_entries_kept(tree, data)
    assert "backend/app/services/auto_queue_eligibility.py::find_eligible_printer" in kept
    assert "backend/app/services/print_scheduler.py::PrintScheduler._get_filament_requirements" in kept


def test_several_router_rows_are_locked_before_a_set_based_write(tree, data):
    """A20: a set-based UPDATE / DELETE of auto_queue_items follows lock_auto_rows in its function, or names why
    not in its entry's ``auto_rows``."""
    assert guard.auto_rows_without_prelock(tree, data) == []


def test_a_row_enters_a_queue_whose_header_held(tree, data):
    """A25: every builder of a printer row re-reads the target header under the lane, or names where it is done."""
    assert guard.rows_built_without_a_header_read(tree, data) == []


def test_the_file_path_lock_has_exactly_four_takers():
    """A26: the one in-process lock taken under database locks is bounded by its takers -- a fifth fails."""
    assert guard.path_lock_takers() == guard.PATH_LOCK_TAKERS


def test_a_halt_has_one_writer():
    """A9 / A13: a header's status set to error / paused only by ``queue_counters.halt_lane`` (or a named exemption)."""
    assert guard.halts_outside_halt_lane() == []


def test_a_core_status_write_voids_the_deferral_proof():
    """A16 / A13 (TA23): a Core status write on print_queue names deferred_claim_token (except defer_claim)."""
    assert guard.status_writes_keeping_the_proof() == []


def test_every_release_names_the_claim_it_ends(tree, data):
    """A9 / A13: no bare ``set_queue_idle(db, q)`` outside L2c -- a release names its row or the header it read."""
    assert guard.bare_releases(tree, data) == []


def _app_with(tmp_path, files: dict[str, str]):
    for rel, source in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(source).lstrip(), encoding="utf-8")
    return tmp_path


def test_negative_control_a_halt_outside_halt_lane(tmp_path):
    app = _app_with(
        tmp_path,
        {
            "services/rogue.py": """
                async def core_halt(db, q):
                    await db.execute(update(PrinterQueue).where(PrinterQueue.id == q).values(status="error"))

                async def orm_halt(queue):
                    queue.status = "paused"

                async def a_release(queue):
                    queue.status = "idle"
            """,
            "services/queue_counters.py": """
                async def halt_lane(db, queue):
                    queue.status = "paused"
            """,
        },
    )
    assert guard.halts_outside_halt_lane(app) == ["services/rogue.py::core_halt:2", "services/rogue.py::orm_halt:5"]


def test_negative_control_a_core_status_write_that_keeps_the_proof(tmp_path):
    app = _app_with(
        tmp_path,
        {
            "services/rogue.py": """
                async def keeps(db, i):
                    await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == i).values(status="cancelled"))

                async def voids(db, i):
                    await db.execute(
                        update(PrintQueueItem).where(PrintQueueItem.id == i).values(
                            status="cancelled", deferred_claim_token=None
                        )
                    )

                async def a_lock(db, i):
                    await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == i).values(status=PrintQueueItem.status))

                async def defer_claim(db, i):
                    await db.execute(update(PrintQueueItem).where(PrintQueueItem.id == i).values(status="pending"))
            """,
        },
    )
    assert guard.status_writes_keeping_the_proof(app) == ["services/rogue.py::keeps:2"]


def test_negative_control_a_fifth_path_lock_taker(tmp_path):
    app = _app_with(
        tmp_path,
        {
            "services/rogue.py": """
                async def fifth(db, path):
                    async with archive_file_reference_scope(db, path):
                        pass
            """,
        },
    )
    assert guard.path_lock_takers(app) == {"services/rogue.py::fifth"}
