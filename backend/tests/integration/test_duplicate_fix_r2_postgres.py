"""Fix-review r2 cases on the same isolated PostgreSQL fixture as other queue guards."""

from backend.tests.integration.test_duplicate_empty_name_recovery import (  # noqa: F401
    test_resolver_and_catch_up_proof_agree_without_a_subtask_name,
)
from backend.tests.integration.test_duplicate_fix_review_repairs import (  # noqa: F401
    telemetry,
    test_batch_clone_refusal_names_the_stale_archive,
    test_ok_owed_group_archive_is_listed_with_settlement,
    test_printing_archive_without_claim_is_still_diagnosed,
    test_swap_hold_still_pauses_the_queue_when_its_archive_was_trashed,
    test_the_printing_archive_that_vetoes_group_repair_is_named_on_the_page,
)
from backend.tests.integration.test_duplicate_publication_outcomes import (  # noqa: F401
    test_attempt_that_leaves_printing_after_the_proof_does_not_cost_b_its_archive,
    test_durable_publication_is_not_reported_unconfirmed_after_a_reconnect,
    test_stale_committed_join_is_handed_to_the_replacement_without_replaying_start_effects,
)
from backend.tests.integration.test_duplicate_terminal_catchup import (  # noqa: F401
    test_reissued_catch_up_after_the_print_ended_adopts_nothing,
)
from backend.tests.integration.test_queue_auto_transfer import test_engine  # noqa: F401
