"""Case folding is Unicode-aware on every backend (core/case_folding.py).

SQLite's built-in lower() knows ASCII only; the app shadows it with Python's
on every connection. A ``C``-locale PostgreSQL folds ASCII only too: it is
probed once at boot, and when it cannot fold, ``ilike``/``lower``/``upper``
compile through a collation that can — tested here without a server, because
the compiler is pure text.
"""

import logging
from unittest.mock import Mock

import pytest
from sqlalchemy import Column, MetaData, String, Table, func, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import OperationalError

from backend.app.core import case_folding


class TestSqliteFunctions:
    def test_lower_folds_cyrillic_and_keeps_null(self):
        assert case_folding.sqlite_lower("ЛАМПА Настільна") == "лампа настільна"
        assert case_folding.sqlite_lower(None) is None

    def test_upper_folds_cyrillic(self):
        assert case_folding.sqlite_upper("ґудзик") == "ҐУДЗИК"
        assert case_folding.sqlite_upper(None) is None

    def test_non_text_passes_through(self):
        # SQLite may hand a BLOB (bytes) or a number to the function; it is not ours to fold.
        assert case_folding.sqlite_lower(b"\x00\x01") == b"\x00\x01"
        assert case_folding.sqlite_lower(5) == 5

    def test_both_names_are_registered_as_deterministic(self):
        """The registration contract, not just the functions: SQLite may only use
        an application function in an indexed expression when it is declared
        deterministic, and it shadows the built-in only under the exact name."""
        conn = Mock()

        case_folding.register_sqlite_functions(conn)

        assert [c.args for c in conn.create_function.call_args_list] == [
            ("lower", 1, case_folding.sqlite_lower),
            ("upper", 1, case_folding.sqlite_upper),
        ]
        assert [c.kwargs for c in conn.create_function.call_args_list] == [
            {"deterministic": True},
            {"deterministic": True},
        ]


@pytest.mark.asyncio
async def test_the_test_engine_lowers_unicode(test_engine):
    async with test_engine.connect() as conn:
        assert (await conn.execute(text("SELECT lower('ЛАМПА')"))).scalar() == "лампа"
        assert (await conn.execute(text("SELECT upper('лампа')"))).scalar() == "ЛАМПА"
        assert (await conn.execute(text("SELECT 'Лампа' LIKE '%' || lower('ЛАМПА') || '%'"))).scalar() == 0
        # The shape SQLAlchemy emits for ilike on SQLite: lower(col) LIKE lower(:q)
        assert (await conn.execute(text("SELECT lower('Лампа настільна') LIKE lower('%ЛАМПА%')"))).scalar() == 1


# The columns m001 indexes, copied from its ``_FTS_COLS`` — the DDL below is
# that migration's, minus the triggers (the row is inserted by hand instead).
_FTS_COLS = "print_name, filename, tags, notes, designer, filament_type"


@pytest.mark.asyncio
async def test_the_sqlite_fts5_index_folds_cyrillic(test_engine):
    """Measure the production SQLite archives path, rather than reason about it.

    On SQLite ``/archives/?search=`` asks the ``archive_fts`` FTS5 index, and the
    claim that its default ``unicode61`` tokenizer folds Cyrillic — on both sides,
    the indexed text and the MATCH term — is why the index branch was never part
    of this bug. It was reasoning until this test; the route's ``ilike`` fallback
    is pinned in ``tests/integration/test_archives_search.py``, which cannot
    reach the index because ``create_all`` does not create a virtual table.
    """
    create_fts = text(f"""
        CREATE VIRTUAL TABLE IF NOT EXISTS archive_fts USING fts5(
            {_FTS_COLS},
            content='print_archives',
            content_rowid='id'
        )
    """)
    try:
        async with test_engine.begin() as conn:
            await conn.execute(create_fts)
    except OperationalError as exc:
        if "no such module: fts5" not in str(exc):
            raise
        pytest.skip(f"this SQLite build has no FTS5, so the index path cannot be measured here: {exc}")

    async with test_engine.begin() as conn:
        # Straight into the index: the triggers are m001's and a real row in
        # ``print_archives`` is not what is under test.
        await conn.execute(
            text(f"""
                INSERT INTO archive_fts(rowid, {_FTS_COLS})
                VALUES (1, 'Кронштейн', 'kronshtein.gcode.3mf', '', '', '', 'PETG')
            """)
        )

    async with test_engine.connect() as conn:

        async def matches(term: str) -> list[int]:
            result = await conn.execute(text("SELECT rowid FROM archive_fts WHERE archive_fts MATCH :t"), {"t": term})
            return [row[0] for row in result.fetchall()]

        assert await matches("кронштейн") == [1], "the lower-case term must find the capitalised row"
        assert await matches("КРОНШТЕЙН") == [1], "and the upper-case term the same row"
        # The guard that makes the two above mean something: a tokenizer that
        # matched everything would pass them both.
        assert await matches("кран") == []


class TestChooser:
    def test_native_needs_no_collation(self):
        assert case_folding.choose_fold_collation(True, {"pg_c_utf8", "und-x-icu"}) is None

    def test_prefers_the_builtin_over_icu(self):
        assert case_folding.choose_fold_collation(False, {"und-x-icu", "pg_c_utf8"}) == "pg_c_utf8"

    def test_falls_back_to_icu(self):
        assert case_folding.choose_fold_collation(False, {"und-x-icu"}) == "und-x-icu"

    def test_nothing_available(self):
        assert case_folding.choose_fold_collation(False, set()) is None


class _FakeResult:
    def __init__(self, value=None, rows=()):
        self._value = value
        self._rows = rows

    def scalar(self):
        return self._value

    def __iter__(self):
        return iter(self._rows)


class _FakeEngine:
    """Just enough engine for ``probe_postgres_case_folding``: ``connect()`` as
    an async context manager, ``execute()`` answering by the SQL it is handed."""

    def __init__(
        self,
        *,
        folds: bool,
        collations: tuple[str, ...] = (),
        collation_folds: bool = True,
        verify_raises: tuple[str, ...] = (),
        fail=None,
        fail_on: str | None = None,
        fail_on_close=None,
    ):
        self._folds = folds
        self._collations = collations
        self._collation_folds = collation_folds
        self._verify_raises = verify_raises
        self._fail = fail
        self._fail_on = fail_on
        self._fail_on_close = fail_on_close
        self.statements: list[str] = []
        self.rollbacks = 0

    def connect(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        # Closing the connection is the one step that can still fail after the
        # collation has been chosen and verified.
        if self._fail_on_close is not None:
            raise self._fail_on_close
        return False

    async def rollback(self):
        self.rollbacks += 1

    async def execute(self, stmt, params=None):
        sql = str(stmt)
        self.statements.append(sql)
        if self._fail is not None:
            raise self._fail
        if self._fail_on is not None and self._fail_on in sql:
            raise RuntimeError(f"the server refused a statement mentioning {self._fail_on}")
        if "COLLATE" in sql:  # before the bare probe: this statement also contains lower('Ж')
            name = next((c for c in case_folding.FOLD_COLLATIONS if f'"{c}"' in sql), None)
            if name is None:
                # A bare ``next()`` would raise StopIteration here, which inside
                # a coroutine surfaces as a RuntimeError from somewhere else
                # entirely and hides the statement that caused it.
                raise AssertionError(f"the probe collated through a collation this stub does not know: {sql!r}")
            if name in self._verify_raises:
                raise RuntimeError(f"could not open collator for locale {name}")
            return _FakeResult(value=self._collation_folds)
        if "lower('Ж')" in sql:
            return _FakeResult(value=self._folds)
        if "datctype" in sql:
            return _FakeResult(value="C")
        if "pg_collation" in sql:
            # The lookup carries the two names as literals — no bound array.
            assert "ANY" not in sql and ":names" not in sql, sql
            assert all(f"'{c}'" in sql for c in case_folding.FOLD_COLLATIONS), sql
            # …and it is qualified to the catalog, so a same-named collation in
            # ``public`` cannot be found through search_path instead.
            assert "pg_catalog" in sql, sql
            return _FakeResult(rows=[(c,) for c in self._collations])
        raise AssertionError(f"the probe asked something unexpected: {sql!r}")


class TestProbe:
    def setup_method(self):
        case_folding.reset_for_tests()

    def teardown_method(self):
        case_folding.reset_for_tests()

    @pytest.mark.asyncio
    async def test_a_folding_database_is_left_alone(self):
        engine = _FakeEngine(folds=True, collations=("pg_c_utf8",))

        await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (True, None)
        assert len(engine.statements) == 1, "one question is enough when the answer is yes"

    @pytest.mark.asyncio
    async def test_a_c_locale_database_picks_the_builtin_collation(self):
        engine = _FakeEngine(folds=False, collations=("pg_c_utf8", "und-x-icu"))

        await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, "pg_c_utf8")

    @pytest.mark.asyncio
    async def test_only_icu_available(self):
        engine = _FakeEngine(folds=False, collations=("und-x-icu",))

        await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, "und-x-icu")

    @pytest.mark.asyncio
    async def test_a_collation_that_does_not_actually_fold_is_rejected(self, caplog):
        """The collation is verified, not just looked up — an ICU collation can
        exist on a build whose ICU is broken, and rendering it would be worse
        than leaving ILIKE alone."""
        engine = _FakeEngine(folds=False, collations=("und-x-icu",), collation_folds=False)

        with caplog.at_level(logging.WARNING, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, None)
        assert "no Unicode collation" in caplog.text

    @pytest.mark.asyncio
    async def test_no_collation_at_all_names_the_cure(self, caplog):
        engine = _FakeEngine(folds=False)

        with caplog.at_level(logging.WARNING, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, None)
        assert "Recreate the database with a UTF-8" in caplog.text

    @pytest.mark.asyncio
    async def test_a_candidate_that_raises_costs_only_its_own_turn(self, caplog):
        """A broken ICU collation errors instead of answering. That is one
        candidate's problem, not the probe's: the next one still gets asked, and
        because PostgreSQL aborts the transaction on a failed statement, the
        rollback in between is what makes the second question possible."""
        engine = _FakeEngine(folds=False, collations=("pg_c_utf8", "und-x-icu"), verify_raises=("pg_c_utf8",))

        with caplog.at_level(logging.INFO, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, "und-x-icu")
        assert engine.rollbacks == 1
        assert "pg_c_utf8" in caplog.text and "trying the next" in caplog.text

    @pytest.mark.asyncio
    async def test_every_candidate_raising_leaves_no_collation_but_keeps_the_answer(self, caplog):
        engine = _FakeEngine(
            folds=False, collations=("pg_c_utf8", "und-x-icu"), verify_raises=("pg_c_utf8", "und-x-icu")
        )

        with caplog.at_level(logging.WARNING, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, None)
        assert "Recreate the database with a UTF-8" in caplog.text

    @pytest.mark.asyncio
    async def test_the_last_candidate_is_not_told_to_try_the_next(self, caplog):
        """Two ways a candidate can drop out — it raises, or it answers "I do not
        fold" — and both used to promise a next attempt that does not exist. The
        line an operator reads has to say which of the two happened."""
        raised = _FakeEngine(folds=False, collations=("und-x-icu",), verify_raises=("und-x-icu",))
        with caplog.at_level(logging.INFO, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(raised)
        assert "no candidate left" in caplog.text and "trying the next" not in caplog.text, caplog.text

        caplog.clear()
        case_folding.reset_for_tests()
        answered = _FakeEngine(folds=False, collations=("und-x-icu",), collation_folds=False)
        with caplog.at_level(logging.INFO, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(answered)
        assert "does not fold non-ASCII case; no candidate left" in caplog.text, caplog.text

    @pytest.mark.asyncio
    async def test_a_failure_after_a_collation_was_chosen_says_folding_is_on(self, caplog):
        """The probe's own tail can fail (closing the connection) after the
        collation has been chosen and verified. Reporting "folds ASCII only"
        there is not a harmless overstatement: it is the line that tells the
        operator to recreate a database whose search works."""
        engine = _FakeEngine(
            folds=False,
            collations=("pg_c_utf8",),
            fail_on_close=RuntimeError("connection was closed in the middle of operation"),
        )

        with caplog.at_level(logging.INFO, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, "pg_c_utf8")
        assert "could not finish cleanly" in caplog.text and "pg_c_utf8" in caplog.text
        assert "folds ASCII only" not in caplog.text
        assert not [r for r in caplog.records if r.levelno >= logging.WARNING], "folding is engaged — not a warning"

    @pytest.mark.asyncio
    async def test_a_failure_after_the_answer_never_claims_the_database_folds(self, caplog):
        """Measured non-folding stands. Collapsing to the native defaults here
        would re-open the FTS gate on a database whose tsvectors hold unfolded
        tokens — searching wrongly and silently."""
        engine = _FakeEngine(folds=False, collations=("pg_c_utf8",), fail_on="datctype")

        with caplog.at_level(logging.WARNING, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (False, None)
        assert "could not finish" in caplog.text
        assert "folds natively" not in caplog.text

    @pytest.mark.asyncio
    async def test_a_failing_probe_never_prevents_boot(self, caplog):
        """Whatever the server says — an old version, a permission error, a
        dropped connection — the app boots with the native defaults. Only the
        first question may end here: it never got an answer at all."""
        engine = _FakeEngine(folds=False, fail=RuntimeError("terminating connection due to administrator command"))

        with caplog.at_level(logging.WARNING, logger="backend.app.core.case_folding"):
            await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (True, None)
        assert "probe failed" in caplog.text

    @pytest.mark.asyncio
    async def test_a_reprobe_that_cannot_ask_inherits_nothing(self):
        """``reinitialize_database`` probes again after a restore. A call that
        never gets its first answer must land on the defaults, not keep the
        previous database's collation."""
        case_folding.pg_native_folds, case_folding.pg_fold_collation = False, "pg_c_utf8"
        engine = _FakeEngine(folds=False, fail=RuntimeError("server closed the connection unexpectedly"))

        await case_folding.probe_postgres_case_folding(engine)

        assert (case_folding.pg_native_folds, case_folding.pg_fold_collation) == (True, None)


def _pg_dialect():
    """The dialect as ``core/database.py`` prepares it — through ``compiler_for``,
    so every test below compiles with a class derived from asyncpg's own
    compiler rather than from a hand-picked base."""
    d = postgresql.asyncpg.dialect()
    d.statement_compiler = case_folding.compiler_for(d)
    return d


def _compile(stmt) -> str:
    return str(stmt.compile(dialect=_pg_dialect(), compile_kwargs={"literal_binds": True}))


_t = Table("t", MetaData(), Column("name", String), Column("notes", String))


class TestCompilerFor:
    """The folding behaviour is a mixin laid over the dialect's OWN compiler, not
    a class assigned over it — measurable on the class, because the compiler
    asyncpg brings is empty and the SQL would look the same either way."""

    def test_the_compiler_the_dialect_carries_is_the_base(self):
        d = postgresql.asyncpg.dialect()
        stock = d.statement_compiler

        folding = case_folding.compiler_for(d)

        assert issubclass(folding, stock), "the driver's compiler must still be in the chain"
        mro = folding.__mro__
        assert mro.index(case_folding.CaseFoldingCompilerMixin) < mro.index(stock), (
            "the mixin must win the method lookup, or ilike would not be folded"
        )

    def test_an_override_the_driver_brought_survives(self):
        """The case that is free today and would not be with a psycopg URL: a
        compiler that carries something of its own keeps it."""
        d = postgresql.asyncpg.dialect()
        d.statement_compiler = type("PGCompiler_pretend", (d.statement_compiler,), {"driver_marker": "kept"})

        assert case_folding.compiler_for(d).driver_marker == "kept"

    def test_asking_twice_does_not_stack_a_second_layer(self):
        """``reinitialize_database`` builds another engine; a dialect that already
        carries the folding compiler must not be wrapped again."""
        d = postgresql.asyncpg.dialect()
        first = case_folding.compiler_for(d)
        d.statement_compiler = first

        assert case_folding.compiler_for(d) is first


class TestCompiler:
    def setup_method(self):
        case_folding.reset_for_tests()

    def teardown_method(self):
        case_folding.reset_for_tests()

    def test_native_database_compiles_as_stock(self):
        case_folding.pg_native_folds, case_folding.pg_fold_collation = True, None
        sql = _compile(select(_t.c.name).where(_t.c.name.ilike("%лампа%")))
        assert "ILIKE" in sql and "COLLATE" not in sql
        sql = _compile(select(func.lower(_t.c.name)))
        assert sql.strip().startswith("SELECT lower(t.name)")

    def test_the_native_form_is_byte_identical_to_the_stock_compiler(self):
        """``_fold_func`` renders the stock one-argument form by hand (calling
        ``visit_function`` from inside ``visit_lower_func`` would recurse), so
        drift from what stock PostgreSQL emits has to be caught by comparison,
        not by eye."""
        case_folding.pg_native_folds, case_folding.pg_fold_collation = True, None
        stock = postgresql.asyncpg.dialect()
        for stmt in (
            select(func.lower(_t.c.name)),
            select(func.upper(_t.c.name)),
            select(func.lower(_t.c.name, _t.c.name)),
            select(func.lower(_t.c.name + _t.c.notes)),
            select(_t.c.name).where(_t.c.name.ilike("%лампа%")),
            select(_t.c.name).where(_t.c.name.not_ilike("50!%", escape="!")),
        ):
            assert _compile(stmt) == str(stmt.compile(dialect=stock, compile_kwargs={"literal_binds": True}))

    def test_ilike_renders_the_collation(self):
        case_folding.pg_native_folds, case_folding.pg_fold_collation = False, "pg_c_utf8"
        sql = _compile(select(_t.c.name).where(_t.c.name.ilike("%лампа%")))
        assert 'lower((t.name) COLLATE "pg_c_utf8") LIKE lower((\'%лампа%\') COLLATE "pg_c_utf8")' in sql

    def test_the_collated_operand_is_parenthesised(self):
        """COLLATE binds tighter than every operator: without the parentheses
        ``lower(t.name || t.notes COLLATE "c")`` collates ``t.notes`` alone and
        folds half the expression."""
        case_folding.pg_native_folds, case_folding.pg_fold_collation = False, "pg_c_utf8"
        assert 'lower((t.name || t.notes) COLLATE "pg_c_utf8")' in _compile(select(func.lower(_t.c.name + _t.c.notes)))

    def test_the_parameterised_shape_casts_before_collating(self):
        """Without ``literal_binds`` — the shape the app actually sends. asyncpg's
        dialect renders the parameter as ``$1::VARCHAR``, and that cast is what
        gives COLLATE a typed expression to apply to."""
        case_folding.pg_native_folds, case_folding.pg_fold_collation = False, "pg_c_utf8"
        sql = str(select(_t.c.name).where(_t.c.name.ilike("%лампа%")).compile(dialect=_pg_dialect()))
        assert 'lower((t.name) COLLATE "pg_c_utf8") LIKE lower(($1::VARCHAR) COLLATE "pg_c_utf8")' in sql

    def test_not_ilike_keeps_escape(self):
        case_folding.pg_native_folds, case_folding.pg_fold_collation = False, "pg_c_utf8"
        sql = _compile(select(_t.c.name).where(_t.c.name.not_ilike("50!%", escape="!")))
        assert "NOT LIKE" in sql and "ESCAPE '!'" in sql and 'COLLATE "pg_c_utf8"' in sql

    def test_lower_and_upper_render_the_collation(self):
        case_folding.pg_native_folds, case_folding.pg_fold_collation = False, "und-x-icu"
        assert 'lower((t.name) COLLATE "und-x-icu")' in _compile(select(func.lower(_t.c.name)))
        assert 'upper((t.name) COLLATE "und-x-icu")' in _compile(select(func.upper(_t.c.name)))

    def test_other_arities_are_untouched(self):
        case_folding.pg_native_folds, case_folding.pg_fold_collation = False, "pg_c_utf8"
        sql = _compile(select(func.lower(_t.c.name, _t.c.name)))
        assert "COLLATE" not in sql


@pytest.mark.asyncio
async def test_the_archives_fts_gate_reads_the_chosen_collation(monkeypatch):
    """The tsvector branch is wrong exactly where the ilike branch is RIGHT — and
    that is the collation, not the native flag.

    Both flags are set in every case because the two answers differ precisely in
    the third row: a database that folds nothing has no collation either, and
    then neither the m001 vectors nor ``ilike`` fold, so the index stays the
    faster half of a search that is ASCII-only whichever branch runs.
    """
    from backend.app.api.routes import archives as archives_route

    monkeypatch.setattr(archives_route, "is_postgres", lambda: True)
    for native, collation, uses_fts in (
        (True, None, True),  # folds itself: the vectors are folded too
        (False, "pg_c_utf8", False),  # the collation folds ilike and not them
        (False, None, True),  # nothing folds: the index still ranks and is faster
    ):
        monkeypatch.setattr(case_folding, "pg_native_folds", native)
        monkeypatch.setattr(case_folding, "pg_fold_collation", collation)
        assert archives_route._use_fts_search() is uses_fts, (native, collation)


class _NestedBlock:
    """``AsyncSession.begin_nested()``'s context manager, reduced to a recorder.

    It never swallows: a real SAVEPOINT block rolls the savepoint back and
    re-raises, and the route's own ``except`` is what decides to fall back.
    With ``fail_on_release`` it models the other way out — the statement inside
    succeeded and leaving the block (RELEASE SAVEPOINT) is what raised.
    """

    def __init__(self, db: "_RecordingDB"):
        self._db = db

    async def __aenter__(self):
        self._db.nested_enters += 1
        return self

    async def __aexit__(self, exc_type, exc, tb):
        self._db.nested_exits.append(exc_type)
        if exc_type is None and self._db.fail_on_release:
            raise RuntimeError("could not release savepoint: server closed the connection unexpectedly")
        return False


class _RecordingDB:
    """Records the SQL a route executes; results are empty unless ``index_rows``
    hands the first statement — the index query — some rowids to return."""

    def __init__(self, *, fail_first: bool = False, fail_on_release: bool = False, index_rows: tuple[int, ...] = ()):
        self.statements: list[str] = []
        self.nested_enters = 0
        self.nested_exits: list[type[BaseException] | None] = []
        self.fail_on_release = fail_on_release
        self._fail_first = fail_first
        self._index_rows = index_rows

    def begin_nested(self):
        return _NestedBlock(self)

    async def execute(self, stmt, params=None):
        self.statements.append(str(stmt))
        if self._fail_first and len(self.statements) == 1:
            raise RuntimeError('syntax error in tsquery: "лампа &"')
        return _Result(self._index_rows if len(self.statements) == 1 else ())


class _Result:
    def __init__(self, rows: tuple[int, ...] = ()):
        self._rows = rows

    def fetchall(self):
        return [(rowid,) for rowid in self._rows]

    def scalars(self):
        return self

    def all(self):
        return []


@pytest.mark.asyncio
async def test_a_search_folded_through_a_collation_never_asks_an_index(monkeypatch):
    """A collation was chosen, so ``ilike`` folds and the m001 vectors do not:
    with the FTS gate shut there is no index to ask (and ``archive_fts`` is
    SQLite's table). Asking anyway would not just waste a round trip — a failing
    statement aborts the PostgreSQL transaction, so the ilike fallback in the
    same handler would fail too."""
    from backend.app.api.routes import archives as archives_route

    monkeypatch.setattr(archives_route, "is_postgres", lambda: True)
    monkeypatch.setattr(case_folding, "pg_native_folds", False)
    monkeypatch.setattr(case_folding, "pg_fold_collation", "pg_c_utf8")
    db = _RecordingDB()

    assert await archives_route.search_archives(q="лампа", db=db, auth_result=(None, True)) == []

    assert len(db.statements) == 1, db.statements
    sql = db.statements[0]
    assert "archive_fts" not in sql and "to_tsquery" not in sql
    assert "FROM print_archives" in sql and "LIKE" in sql.upper()


@pytest.mark.asyncio
async def test_a_postgres_that_folds_nothing_keeps_its_index(monkeypatch):
    """The third case, and the one the gate used to get wrong: the database folds
    ASCII only and this server has no Unicode collation either, so nothing folds
    on either branch. Turning the index off buys no correctness — it only trades
    a ranked GIN lookup for a six-column ``%q%`` scan."""
    from backend.app.api.routes import archives as archives_route

    monkeypatch.setattr(archives_route, "is_postgres", lambda: True)
    monkeypatch.setattr(case_folding, "pg_native_folds", False)
    monkeypatch.setattr(case_folding, "pg_fold_collation", None)
    db = _RecordingDB()

    assert await archives_route.search_archives(q="лампа", db=db, auth_result=(None, True)) == []

    assert len(db.statements) == 1, db.statements
    assert "to_tsquery" in db.statements[0]


@pytest.mark.asyncio
async def test_the_sqlite_search_still_goes_through_fts5(monkeypatch):
    """The symmetric case, so the gate cannot quietly divert the default backend:
    SQLite has a real ``archive_fts`` index and its own folded ``lower``, so it
    keeps using it."""
    from backend.app.api.routes import archives as archives_route

    monkeypatch.setattr(archives_route, "is_postgres", lambda: False)
    db = _RecordingDB()

    assert await archives_route.search_archives(q="лампа", db=db, auth_result=(None, True)) == []

    assert len(db.statements) == 1, db.statements
    sql = db.statements[0]
    assert "archive_fts" in sql and "to_tsquery" not in sql
    # The savepoint is not a PostgreSQL-only detail of the handler: the index
    # query runs inside one on every backend, and on the happy path the block
    # is entered and left without an exception.
    assert (db.nested_enters, db.nested_exits) == (1, [None])


@pytest.mark.asyncio
async def test_a_failing_index_query_only_rolls_back_its_own_savepoint(monkeypatch):
    """A malformed ``to_tsquery`` is a statement the server refuses, and on
    PostgreSQL a refused statement aborts the whole transaction — so the ilike
    fallback in the same handler would be refused too ("current transaction is
    aborted"), turning a bad search string into a 500. A SAVEPOINT around the
    index query is what keeps the fallback reachable; ``db.rollback()`` is not
    the alternative, it would expire every object the request has already
    loaded (the authenticated user among them).
    """
    from backend.app.api.routes import archives as archives_route

    monkeypatch.setattr(archives_route, "is_postgres", lambda: True)
    monkeypatch.setattr(case_folding, "pg_native_folds", True)
    monkeypatch.setattr(case_folding, "pg_fold_collation", None)
    db = _RecordingDB(fail_first=True)

    assert await archives_route.search_archives(q="лампа", db=db, auth_result=(None, True)) == []

    assert db.nested_enters == 1, "the index query must run inside a SAVEPOINT"
    assert db.nested_exits == [RuntimeError], "the failure must leave the savepoint block, not be swallowed inside it"
    assert len(db.statements) == 2, db.statements
    assert "to_tsquery" in db.statements[0]
    fallback = db.statements[1]
    assert "FROM print_archives" in fallback and "lower(" in fallback


@pytest.mark.asyncio
async def test_an_index_query_whose_savepoint_fails_on_release_also_falls_back(monkeypatch, caplog):
    """The other way out of the block: the index SELECT answered, and leaving it
    (RELEASE SAVEPOINT — a dropped connection, a server shutting down) is what
    raised. ``matched_ids`` is assigned after the block for exactly this. Filled
    in inside it, it would already hold the rowids when the handler logs its
    fallback, and the request would then answer from the index branch it has just
    said it is giving up on — the log and the answer telling different stories.
    """
    from backend.app.api.routes import archives as archives_route

    monkeypatch.setattr(archives_route, "is_postgres", lambda: False)
    db = _RecordingDB(fail_on_release=True, index_rows=(7,))

    with caplog.at_level(logging.WARNING, logger="backend.app.api.routes.archives"):
        assert await archives_route.search_archives(q="лампа", db=db, auth_result=(None, True)) == []

    assert db.nested_exits == [None], "the SELECT itself did not raise — leaving the block did"
    assert "falling back to LIKE search" in caplog.text
    assert len(db.statements) == 2, db.statements
    assert "archive_fts" in db.statements[0]
    fallback = db.statements[1]
    assert "FROM print_archives" in fallback and "lower(" in fallback
    assert "IN (" not in fallback, "fetching the matched ids would mean the index rows were used after all"
