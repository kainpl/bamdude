"""The live authority contract on a disposable native PostgreSQL server."""

import pytest

from backend.tests.integration.test_embedded_postgres_live import live_settings  # noqa: F401
from backend.tests.integration.test_g01_a_postgres import test_engine  # noqa: F401
from backend.tests.unit.services.test_g01_d_live_authority import (
    live,  # noqa: F401
    test_admission_does_not_join_batch_started_before_its_accept as assert_fresh_admission,
    test_bulk_reads_have_same_predicate_and_query_count_for_more_identical_viewers as assert_batch,
    test_exact_logout_closes_only_that_sid_after_current_read as assert_exact_logout,
    test_long_lived_admission_and_recheck_preserve_the_verified_scope as assert_ll_scope,
    test_parent_deadline_bounds_memory_delivery_even_when_child_lives_longer as assert_parent_deadline,
    test_reused_numeric_row_id_never_rebinds_pending_or_admitted_capture as assert_identity_reuse,
    test_unknown_commit_then_close_requires_new_admission_epoch as assert_unknown_outcome,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.asyncio]


async def test_exact_logout_on_postgres(live):
    await assert_exact_logout(live)


async def test_bulk_predicate_and_query_count_on_postgres(live, test_engine):
    await assert_batch(live, test_engine)


async def test_final_read_after_accept_on_postgres(live, test_engine, monkeypatch):
    await assert_fresh_admission(live, test_engine, monkeypatch)


async def test_parent_expiry_on_postgres(live):
    await assert_parent_deadline(live)


async def test_unknown_outcome_on_postgres(live, test_engine):
    await assert_unknown_outcome(live, test_engine)


@pytest.mark.parametrize("kind", ["account", "key", "printer"])
async def test_identity_reuse_on_postgres(live, kind):
    await assert_identity_reuse(live, kind)


async def test_ll_scope_on_postgres(live):
    await assert_ll_scope(live)
