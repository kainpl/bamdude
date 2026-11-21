from backend.app.models.print_queue import PrintQueueItem
from backend.app.services.auto_queue_placement_basis import PLACEMENT_COLUMNS, placement_basis


def test_every_printer_queue_column_is_classified():
    excluded = {
        # Row and lineage identities, checked separately under the return locks.
        "id",
        "recovery_identity",
        "swap_declaration_identity",
        "source_auto_identity",
        "source_auto_item_id",
        "placement_basis",
        "returned_receipt_identity",
        # Eligibility gates, independent of the placement intent.
        "origin",
        "is_calibration",
        "calibration_session_id",
        # Claim echoes and navigational links that the dispatcher can rewrite.
        "ams_mapping",
        "use_ams",
        "plate_id",
        "archive_id",
        "library_file_id",
        # Queue order and grouping do not change print intent.
        "position",
        "batch_id",
        # Lifecycle fields and diagnostics.
        "status",
        "started_at",
        "completed_at",
        "created_at",
        "deferred_claim_token",
        "dispatch_attempts",
        "gate_acknowledged",
        "error_code",
        "error_message",
        "waiting_reason",
        "waiting_reason_code",
        "waiting_reason_checked_at",
    }
    included = set(PLACEMENT_COLUMNS)
    assert included.isdisjoint(excluded)
    assert included | excluded == set(PrintQueueItem.__table__.columns.keys())


def test_dispatch_echoes_and_reordering_do_not_change_the_basis():
    row = PrintQueueItem(queue_id=1, filament_routing='{"version":2,"mode":"auto"}', flow_cali_mode="auto")
    original = placement_basis(row)
    assert original.startswith("v1:") and len(original) == 67
    row.filament_routing = '{"mode":"auto", "runtime":{"state":"ready"}, "version":2}'
    row.position = 2
    row.batch_id = "group"
    row.ams_mapping = "[5]"
    row.plate_id = 3
    row.use_ams = False
    row.archive_id = 4
    assert placement_basis(row) == original
    row.flow_cali_mode = "off"
    assert placement_basis(row) != original
