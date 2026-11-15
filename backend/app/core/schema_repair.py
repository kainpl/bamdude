"""The sole durable import repair owner, shared by startup and offline retry."""

import logging
import re
from datetime import datetime

from backend.app.core import schema_conform_repair as conform
from backend.app.core.schema_index_repair import repair_indexes, retry_index_residuals
from backend.app.core.schema_repair_state import (
    AUDIT,
    SchemaRepairPending,
    claim_attempt,
    fail_attempt,
    finish_attempt,
    read_record,
)

logger = logging.getLogger(__name__)


async def claim(engine, metadata, *, automatic=True):
    # COMMIT before any phase: a killed process has spent this attempt.
    async with engine.begin() as conn:
        return await claim_attempt(conn, metadata, automatic=automatic)


async def _accepted(conn, debt):
    if debt["acceptance"] is None:
        return None
    if debt["acceptance"] != AUDIT + debt["id"]:
        raise SchemaRepairPending("invalid_acceptance_owner")
    audit = await read_record(conn, debt["acceptance"])
    if audit is None or audit.get("restore_id") != debt["id"]:
        raise SchemaRepairPending("missing_acceptance_audit")
    # Only server-issued records from the replacement transaction authorize
    # this exact object, never an arbitrary imported setting/boolean.
    expected_keys = {
        "version",
        "restore_id",
        "actor",
        "zip_sha256",
        "source_sha256",
        "target",
        "accepted_object",
        "duplicate_groups",
        "contract",
        "confirmed_at",
    }
    actor = audit.get("actor")
    if (
        set(audit) != expected_keys
        or audit.get("accepted_object") != "filament_calibration.indexes.ux_filament_cali_active"
        or audit.get("contract") != "d4-v1"
        or type(audit.get("duplicate_groups")) is not int
        or audit["duplicate_groups"] <= 0
        or not isinstance(actor, dict)
        or actor.get("kind") != "session"
        or not isinstance(audit.get("target"), list)
        or len(audit["target"]) != 3
        or not all(audit["target"])
        or not isinstance(audit.get("source_sha256"), list)
        or len(audit["source_sha256"]) != 2
    ):
        raise SchemaRepairPending("invalid_acceptance_audit")
    hashes = [audit.get("zip_sha256"), audit["source_sha256"][0]]
    if audit["source_sha256"][1] is not None:
        hashes.append(audit["source_sha256"][1])
    if not all(isinstance(h, str) and re.fullmatch("[0-9a-f]{64}", h) for h in hashes):
        raise SchemaRepairPending("invalid_acceptance_audit")
    try:
        if datetime.fromisoformat(audit["confirmed_at"]).tzinfo is None:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise SchemaRepairPending("invalid_acceptance_audit") from None
    return audit


async def run_claimed(engine, metadata, debt, *, phase_hook=None):
    """Resume ALL phases, then verify and exact-owner clear atomically.

    An injected observer belongs to the addressed crash harness; production
    passes none. Labels follow committed phases; "verified" deliberately
    observes the final transaction before its owner can be cleared.
    """

    async def phase(label, operation):
        async with engine.begin() as conn:
            await operation(conn)
        if phase_hook is not None:
            await phase_hook(label)

    try:
        async with engine.begin() as conn:
            accepted = await _accepted(conn, debt)
        if engine.dialect.name == "postgresql":
            from backend.app.migrations.m228_postgres_historical_schema import replay

            await phase("B", replay)
            await phase("D4-indexes", lambda conn: repair_indexes(conn, accepted_calibration=accepted))
            await phase("columns", lambda conn: conform.columns(conn, metadata))
            await phase("UNIQUE", lambda conn: conform.unique(conn, metadata))
            await phase("FK", lambda conn: conform.foreign_keys(conn, metadata))
            await phase(
                "model-indexes", lambda conn: conform.model_indexes(conn, metadata, accepted_calibration=accepted)
            )
            await phase("CHECK", lambda conn: conform.checks(conn, metadata))
            async with engine.begin() as conn:
                results = await conform.verify(conn, metadata, accepted_calibration=accepted)
                _check_accepted_count(accepted, results)
                _annotate_acceptance(debt, accepted, results)
                if phase_hook is not None:
                    await phase_hook("verified")
                await finish_attempt(conn, debt, results)
        else:
            # SQLite already carries its source-level schema. This debt owns
            # D4's additive repairs, not an unsupported SQLite ALTER conform.
            async with engine.begin() as conn:
                results = await repair_indexes(conn, accepted_calibration=accepted)
            if phase_hook is not None:
                await phase_hook("D4-indexes")
            async with engine.begin() as conn:
                # Reinspect in the final transaction; never clear on an old
                # repair return value while the current shape has changed.
                results = await repair_indexes(conn, accepted_calibration=accepted)
                _check_accepted_count(accepted, results)
                _annotate_acceptance(debt, accepted, results)
                if phase_hook is not None:
                    await phase_hook("verified")
                await finish_attempt(conn, debt, results)
        logger.info("Database import repair verified and completed")
    except Exception as exc:
        reason = exc.reason if isinstance(exc, SchemaRepairPending) else "technical_repair_failure"
        try:
            async with engine.begin() as conn:
                await fail_attempt(conn, debt, reason)
        except Exception:
            logger.warning("Repair failure budget could not be committed; existing ownership retained")
        raise SchemaRepairPending(reason) from None


async def retry_residuals(engine, metadata=None):
    async with engine.begin() as conn:
        await retry_index_residuals(conn)
        if metadata is not None and conn.dialect.name == "postgresql":
            from backend.app.core.schema_residual_retry import retry_data_residuals

            await retry_data_residuals(conn, metadata)


def _check_accepted_count(accepted, results):
    for result in results:
        if result["object"] == "filament_calibration.indexes.ux_filament_cali_active":
            if accepted is None or accepted["duplicate_groups"] != result["count"]:
                raise SchemaRepairPending("acceptance_residual_changed")


def _annotate_acceptance(debt, accepted, results):
    if accepted is not None:
        for result in results:
            if result["object"] == accepted["accepted_object"]:
                result.update(provenance="accepted-restore", acceptance=debt["acceptance"])
