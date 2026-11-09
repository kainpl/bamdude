"""Run the review-r1 fixes on a disposable PostgreSQL with independent sessions."""

from backend.tests.integration.test_duplicate_name_recovery import (  # noqa: F401
    main_db,
    test_sole_name_recovery_claims_the_archive_the_resolver_found,
)
from backend.tests.integration.test_duplicate_repair_boundaries import (  # noqa: F401
    telemetry,
    test_f1_archive_kind_release_bypasses_header_effect_veto,
    test_f1_http_accepts_kind_archive_for_old_actions,
    test_f2_superseded_owed_archive_is_listed_and_settlement_offered,
    test_f3_claim_origin_is_never_projected,
    test_verify_ac36_settled_carrier_is_never_sent_by_any_reader,
)
from backend.tests.integration.test_duplicate_repeat_consumers import (  # noqa: F401
    test_operator_cancel_makes_repeat_pick_the_stale_row_over_the_real_one,
    test_plate_repeat_never_rearms_a_stale_claim,
    test_spoolman_snapshot_survives_duplicate_claims_without_choosing_options,
    test_terminal_effect_stage_still_records_the_addressed_archive,
)
from backend.tests.integration.test_duplicate_start_callbacks import (  # noqa: F401
    test_an_attempt_accepted_after_the_proof_must_be_reclassified,
    test_failed_publication_commit_leaves_an_explained_hold,
    test_foreign_claim_refuses_before_start_admission_and_effects,
    test_foreign_start_during_upload_preserves_our_unsent_job,
    test_foreign_start_upload_conflict_through_mqtt_callback_and_dispatch_deferral,
    test_lost_publication_ack_binds_b_and_accepts_its_terminal,
    test_reconnect_before_publication_preserves_the_new_run,
    test_reconnect_during_start_download_preserves_attach_and_spoolman,
    test_refusal_after_admission_pauses_and_explains,
    test_sole_live_archive_uses_the_same_recovery_matcher,
)
from backend.tests.integration.test_queue_auto_transfer import test_engine  # noqa: F401
