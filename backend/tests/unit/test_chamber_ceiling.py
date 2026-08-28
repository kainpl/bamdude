"""The flat chamber ceiling, pinned to the machines we actually ship configs for.

⚠️ Two different questions, and only one of them is answered by a constant.

The manual chamber control asks a **printer**: ``limits_for`` reads that
model's ``support_chamber_temp_edit_range`` out of the mirrored BambuStudio
config, so an X1E is held to its own 60 while an H2D goes to 65. That is the
better answer and this constant must never replace it.

The preheat surfaces have no printer to ask. The filament map is one chamber
target per filament type for the whole farm, and the per-print override is
entered before a printer is chosen. Those take the highest ceiling any model
has and let the firmware clamp on the rest.

Adapted from upstream #2801's sibling commit, which raised a flat 60 to a flat
65 everywhere including the manual control. We keep the per-model answer where
we have one, so this pins only the fallback — and pins it to the configs rather
than to the number 65, so that a BambuStudio re-sync raising a model's range
fails here instead of going unnoticed.
"""

import json
from pathlib import Path

import pytest

from backend.app.utils.temperature_limits import MAX_CHAMBER_TEMP_C, chamber_limits

CONFIG_DIR = Path(__file__).resolve().parents[2] / "app" / "data" / "printers"


def _ranges() -> dict[str, list[int]]:
    """Every ``support_chamber_temp_edit_range`` in the mirrored configs.

    Walks the whole document: BS nests the key inside the config's own blocks,
    and a top-level lookup finds nothing at all.
    """

    def walk(node):
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "support_chamber_temp_edit_range":
                    yield value
                else:
                    yield from walk(value)
        elif isinstance(node, list):
            for value in node:
                yield from walk(value)

    found: dict[str, list[int]] = {}
    for path in sorted(CONFIG_DIR.glob("*.json")):
        for value in walk(json.loads(path.read_text(encoding="utf-8"))):
            found[path.stem] = value
    return found


class TestTheCeilingComesFromTheConfigs:
    def test_the_configs_actually_carry_the_key(self):
        """If this ever empties, every assertion below passes vacuously."""
        assert _ranges(), f"no chamber ranges found under {CONFIG_DIR}"

    def test_it_is_the_highest_any_model_allows(self):
        highest = max(value[1] for value in _ranges().values())
        assert highest == MAX_CHAMBER_TEMP_C, (
            f"the mirrored configs now top out at {highest} °C — raise "
            f"MAX_CHAMBER_TEMP_C on both sides (backend/app/utils/temperature_limits.py "
            f"and frontend/src/utils/printer.ts) or explain why not"
        )

    def test_no_model_is_left_above_it(self):
        over = {code: value for code, value in _ranges().items() if value[1] > MAX_CHAMBER_TEMP_C}
        assert not over, f"these models can go hotter than the flat ceiling: {over}"

    def test_the_frontend_mirror_agrees(self):
        """The two sides bound the same field; drifting apart means the API
        refuses what the input offered."""
        source = (Path(__file__).resolve().parents[3] / "frontend" / "src" / "utils" / "printer.ts").read_text(
            encoding="utf-8"
        )
        assert f"export const MAX_CHAMBER_TEMP_C = {MAX_CHAMBER_TEMP_C};" in source


class TestThePerModelAnswerIsUnchanged:
    def test_a_lower_ceiling_model_keeps_its_own(self):
        """⚠️ The X1E tops out at 60 and must not be raised to the flat number —
        the whole point of keeping ``chamber_limits`` per model."""
        assert chamber_limits([0, 60]) == (0, 60)

    def test_a_model_with_no_config_falls_back_rather_than_erroring(self):
        low, high = chamber_limits(None)
        assert low == 0
        assert high <= MAX_CHAMBER_TEMP_C


@pytest.mark.parametrize(
    "schema_path",
    [
        "backend/app/schemas/print_queue.py",
        "backend/app/schemas/print_options_preference.py",
    ],
)
def test_no_schema_still_carries_the_old_literal(schema_path):
    """The bound is repeated across four fields; one left behind refuses a value
    the other three accept, and the field it refuses is per-print."""
    source = (Path(__file__).resolve().parents[3] / schema_path).read_text(encoding="utf-8")
    assert "preheat_chamber_target_override: int | None = Field(default=None, ge=0, le=60)" not in source
