"""The SQLite -> PostgreSQL move reflects the FILE's schema and translates only what PostgreSQL refuses.

No PostgreSQL here: the reflected schema is compiled with the PostgreSQL
dialect and the DDL text is inspected. Each case is something a real file
did to the importer on 2026-09-09.
"""

import sqlite3

import pytest
from sqlalchemy import JSON, Boolean, Column, Integer, MetaData, String, Table, create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable

from backend.app.core.db_portable import _fk_rule, _pg_predicate, _reflect_sqlite_schema


def _models() -> MetaData:
    md = MetaData()
    Table(
        "widgets",
        md,
        Column("id", Integer, primary_key=True),
        Column("is_active", Boolean, nullable=False),
        Column("key_hash", String(255)),
        Column("payload", JSON),
    )
    return md


def _file(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE widgets (
            id INTEGER NOT NULL,
            is_active INTEGER NOT NULL DEFAULT 1,
            key_hash VARCHAR(64),
            payload TEXT,
            retired_flag BOOLEAN DEFAULT 0,
            stamp DATETIME,
            PRIMARY KEY (id),
            CONSTRAINT ck_active_needs_hash CHECK (is_active = 0 OR key_hash IS NOT NULL)
        );
        CREATE UNIQUE INDEX ux_widgets_active ON widgets (key_hash) WHERE is_active = 1;
        """
    )
    con.commit()
    con.close()
    return path


class TestPredicateTranslation:
    def test_boolean_tests_against_0_and_1_become_true_and_false(self):
        booleans = {"is_active", "auto_link_existing_accounts", "require_email_verified"}
        assert _pg_predicate("is_active = 1", booleans) == "is_active = true"
        assert _pg_predicate("is_active = '1'", booleans) == "is_active = true"
        assert _pg_predicate("is_active != 0", booleans) == "is_active != false"
        assert (
            _pg_predicate(
                "auto_link_existing_accounts = 0 OR email_claim != 'email' OR require_email_verified = 1", booleans
            )
            == "auto_link_existing_accounts = false OR email_claim != 'email' OR require_email_verified = true"
        )

    def test_only_the_tables_own_booleans_are_touched(self):
        # ``count`` is an integer: its ``= 1`` must survive.
        assert _pg_predicate("archived = 0 AND count = 1", {"archived"}) == "archived = false AND count = 1"
        assert _pg_predicate("user_id IS NULL", {"is_active"}) == "user_id IS NULL"
        assert _pg_predicate("origin = 'adhoc_plate'", {"is_active"}) == "origin = 'adhoc_plate'"
        assert _pg_predicate("is_active = 1", set()) == "is_active = 1"

    def test_fk_rule_treats_none_and_no_action_alike(self):
        assert _fk_rule(None) == "NO ACTION"
        assert _fk_rule("no action") == "NO ACTION"
        assert _fk_rule("SET NULL") == "SET NULL"


class TestReflectedSchema:
    def test_model_columns_take_the_models_type_and_the_rest_are_translated(self, tmp_path):
        reflected = _reflect_sqlite_schema(_file(tmp_path), _models())
        ddl = str(CreateTable(reflected.tables["widgets"]).compile(dialect=postgresql.dialect()))

        # the model's Boolean over the file's INTEGER, the model's width over the file's
        assert "is_active BOOLEAN" in ddl
        assert "key_hash VARCHAR(255)" in ddl
        assert "payload JSON" in ddl
        # a column the model no longer knows keeps its reflected type, in PostgreSQL's spelling
        assert "retired_flag BOOLEAN" in ddl
        assert "stamp TIMESTAMP" in ddl
        assert "DATETIME" not in ddl

    def test_boolean_defaults_and_predicates_are_spelled_for_postgres(self, tmp_path):
        reflected = _reflect_sqlite_schema(_file(tmp_path), _models())
        table = reflected.tables["widgets"]
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))

        assert "is_active BOOLEAN DEFAULT true NOT NULL" in ddl
        assert "retired_flag BOOLEAN DEFAULT false" in ddl
        assert "CHECK (is_active = false OR key_hash IS NOT NULL)" in ddl

        index = next(i for i in table.indexes if i.name == "ux_widgets_active")
        index_ddl = str(CreateIndex(index).compile(dialect=postgresql.dialect()))
        assert index_ddl.strip().endswith("WHERE is_active = true")
        assert "UNIQUE" in index_ddl


class TestConstraintAndIndexOfOneName:
    def test_an_index_named_like_a_unique_constraint_is_not_created_twice(self, tmp_path):
        """``create_all`` writes the named constraint, a migration's ``CREATE UNIQUE INDEX IF NOT
        EXISTS`` adds an index of the same name beside it (SQLite keeps the constraint's own as
        an autoindex). PostgreSQL names the constraint's index after the constraint, so the
        second one must not come across."""
        path = tmp_path / "twice.db"
        con = sqlite3.connect(path)
        con.executescript(
            """
            CREATE TABLE notes (
                id INTEGER NOT NULL,
                dedupe_key VARCHAR(160),
                owner VARCHAR(40),
                PRIMARY KEY (id),
                CONSTRAINT uq_notes_dedupe_key UNIQUE (dedupe_key)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_notes_dedupe_key ON notes (dedupe_key);
            CREATE INDEX ix_notes_owner ON notes (owner);
            """
        )
        con.commit()
        con.close()
        table = _reflect_sqlite_schema(path, MetaData()).tables["notes"]
        ddl = str(CreateTable(table).compile(dialect=postgresql.dialect()))
        assert "CONSTRAINT uq_notes_dedupe_key UNIQUE (dedupe_key)" in ddl
        assert [i.name for i in table.indexes] == ["ix_notes_owner"]


@pytest.mark.parametrize("duplicate_index", [False, True])
def test_real_failed_return_receipt_reflects_uniques_and_lookup_index(tmp_path, duplicate_index):
    from backend.app.core.database import Base
    from backend.app.models.auto_queue_return import AutoQueueReturn

    path = tmp_path / "receipt.db"
    engine = create_engine(f"sqlite:///{path}")
    try:
        AutoQueueReturn.__table__.create(engine)
    finally:
        engine.dispose()
    if duplicate_index:
        with sqlite3.connect(path) as conn:
            conn.execute(
                "CREATE UNIQUE INDEX uq_aq_return_attempt_identity "
                "ON auto_queue_returns (old_job_identity, execution_archive_identity)"
            )
    reflected = _reflect_sqlite_schema(path, Base.metadata).tables["auto_queue_returns"]
    ddl = str(CreateTable(reflected).compile(dialect=postgresql.dialect()))
    assert "CONSTRAINT uq_aq_return_attempt_identity UNIQUE (old_job_identity, execution_archive_identity)" in ddl
    assert "CONSTRAINT uq_aq_return_auto_ordinal UNIQUE (auto_identity, ordinal)" in ddl
    assert "CONSTRAINT ck_aq_return_publication CHECK" in ddl
    assert set(reflected.c.keys()) == set(AutoQueueReturn.__table__.c.keys())
    assert {i.name for i in reflected.indexes} == {"ix_aq_return_ready_time"}
    index_ddl = str(CreateIndex(next(iter(reflected.indexes))).compile(dialect=postgresql.dialect()))
    assert "ix_aq_return_ready_time ON auto_queue_returns (returned_at, ready_at, id)" in index_ddl
