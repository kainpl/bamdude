"""Run/source-owned archive metadata must not migrate into newly sliced artifacts."""

from backend.app.services.layer_source import MARKER as LAYER_SOURCE_MARKER

RUN_METADATA_KEYS = frozenset(
    {
        LAYER_SOURCE_MARKER,
        "dispatch_intent",
        "bamdude_terminal_acceptance",
        "bamdude_terminal_effects",
    }
)


def without_run_identity(metadata: dict) -> dict:
    return {key: value for key, value in metadata.items() if key not in RUN_METADATA_KEYS}
