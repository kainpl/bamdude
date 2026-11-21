"""Placement-owned values that must survive unchanged for an unsent return (G03 B2)."""

from backend.app.services.filament_policy import decode
from backend.app.services.print_state_recovery_preview import digest

PLACEMENT_COLUMNS = (
    "queue_id",
    "queue_source_id",
    "source_snapshot",
    "project_id",
    "project_line_id",
    "created_by_id",
    "scheduled_time",
    "manual_start",
    "require_previous_success",
    "auto_off_after",
    "auto_eject",
    "auto_eject_settings",
    "filament_routing",
    "nozzle_mapping",
    "nozzle_rack_choice",
    "bed_levelling",
    "bed_levelling_mode",
    "flow_cali",
    "flow_cali_mode",
    "layer_inspect",
    "timelapse",
    "timelapse_storage",
    "nozzle_offset_cali",
    "nozzle_offset_cali_mode",
    "mesh_mode_fast_check",
    "gcode_injection",
    "execute_swap_macros",
    "swap_macro_events",
    "selected_macro_ids",
    "preheat_override",
    "preheat_chamber_target_override",
)


def placement_basis(row) -> str:
    """Hash the saved intent without the dispatcher-owned routing latch."""
    values = {column: getattr(row, column) for column in PLACEMENT_COLUMNS}
    routing = decode(row.filament_routing, row.filament_routing)
    if isinstance(routing, dict):
        routing = {key: value for key, value in routing.items() if key != "runtime"}
    values["filament_routing"] = routing
    return "v1:" + digest(values)
