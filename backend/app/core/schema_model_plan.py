"""Build a server-normalised model plan without changing the installation.

Only the recovery owner's connection creates empty temporary tables inside a
rolled-back savepoint. PostgreSQL supplies its own names, literal defaults,
CHECK deparsing and dialect options. No backup SQL/default is evaluated here.
The caller captures the real public catalogue BEFORE building this plan.
"""

from copy import deepcopy

from sqlalchemy import CheckConstraint, Column, Index, text
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.schema import AddConstraint, CreateIndex, CreateTable

from backend.app.core.pg_schema_contract import capture, default_key


async def expected_model_plan(conn, metadata):
    from backend.app.core.db_portable import _portable_metadata

    temporary = _portable_metadata(metadata)
    temporary.remove(temporary.tables["_migrations"])
    oidc = temporary.tables.get("oidc_providers")
    if oidc is not None:
        # m031 replaces this model CHECK on every PG chain, including fresh.
        # Preserve its frozen post-chain spelling, not an extra accepted drift
        # pair or broad boolean-expression normalisation.
        from backend.app.migrations.m031_oidc_email_claim import _CHECK_FORMULA, _CONSTRAINT_NAME

        for constraint in list(oidc.constraints):
            if isinstance(constraint, CheckConstraint) and constraint.name == _CONSTRAINT_NAME:
                oidc.constraints.remove(constraint)
                oidc.append_constraint(CheckConstraint(_CHECK_FORMULA, name=_CONSTRAINT_NAME))
    archive = temporary.tables.get("print_archives")
    if archive is not None:
        if "search_vector" not in archive.c:
            archive.append_column(Column("search_vector", TSVECTOR()))
        Index("idx_archives_search_vector", archive.c.search_vector, postgresql_using="gin")
    savepoint = await conn.begin_nested()
    try:
        for table in temporary.tables.values():
            table._prefixes = ["TEMPORARY"]
            await conn.execute(CreateTable(table, include_foreign_key_constraints=[]))
        for table in temporary.tables.values():
            for constraint in table.foreign_key_constraints:
                await conn.execute(AddConstraint(constraint))
            for index in table.indexes:
                await conn.execute(CreateIndex(index))
        namespace = await conn.scalar(text("SELECT pg_my_temp_schema()::regnamespace::text"))
        result = await capture(conn, namespace=namespace)
        if not set(temporary.tables) <= result["schema"].keys():
            raise RuntimeError("Model verification plan did not capture every owned temporary table")
        result["schema"] = {k: v for k, v in result["schema"].items() if k in temporary.tables}
        # Construct the public plan from our OWN temp objects only. This is
        # not a normalisation rule for arbitrary observed source definitions.
        result = deepcopy(result)
        for table in result["schema"].values():
            for column in table["columns"]:
                raw = column["default_raw"]
                if raw and raw.startswith("nextval('") and raw.endswith("'::regclass)"):
                    column["default_raw"] = raw.replace('"' + namespace + '".', "", 1).replace(namespace + ".", "", 1)
                    column["default"] = default_key(column["default_raw"])
            for fk in table["foreign_keys"]:
                if fk.get("referred_schema") == namespace:
                    fk["referred_schema"] = None
        for sequence in result["extra"]["sequences"]:
            sequence["schemaname"] = "public"
        # FTS function/trigger identity and body have their own frozen B
        # contract; they are not model-declared temp objects.
        return result
    finally:
        await savepoint.rollback()
