"""What the user typed in the settings panel beats everything under it.

Three things write into the process JSON before it goes out as
``--load-settings``, and the order is the whole point:

1. the picked process preset is the base;
2. the source 3MF's support configuration layers on top;
3. the designer's own carried tweaks layer on that;
4. **the user's explicit edits win over all of them.**

⚠️ Anything else silently discards a setting somebody just typed — the one
change in the chain that is unambiguously deliberate.
"""

from __future__ import annotations

import inspect
import json

from backend.app.api.routes import library
from backend.app.services.process_overrides import apply_process_overrides


class TestTheOrderInTheRoute:
    @staticmethod
    def _source() -> str:
        return inspect.getsource(library)

    def test_user_overrides_are_applied_after_the_support_carry(self):
        source = self._source()
        support = source.index("_patch_process_support_settings(presets[")
        user = source.index("apply_process_overrides(presets[")
        assert support < user

    def test_and_after_the_designers_carried_tweaks(self):
        source = self._source()
        design = source.index("apply_design_overrides(")
        user = source.index("apply_process_overrides(presets[")
        assert design < user

    def test_they_apply_to_every_model_type_not_just_3mf(self):
        """⚠️ Unlike the two patches above, this reads nothing out of the source
        file — it is what the user typed, so an STL slice must carry it too.

        Asserted on INDENTATION: the guard is `if is_3mf:`, so a call nested
        under it would sit two levels deep. At the function's own level it
        cannot be inside one.
        """
        for line in self._source().splitlines():
            if line.lstrip().startswith("if request.process_overrides:"):
                assert len(line) - len(line.lstrip()) == 4, f"nested under a guard: {line!r}"
                break
        else:
            raise AssertionError("the override call is not in the route at all")


class TestApplyingThem:
    def test_a_value_lands_in_the_process_json(self):
        out = apply_process_overrides(json.dumps({"layer_height": "0.2"}), {"layer_height": 0.28})

        assert json.loads(out)["layer_height"] == "0.28"

    def test_it_overwrites_what_the_support_carry_put_there(self):
        """The precedence, exercised rather than asserted on source order."""
        after_carry = json.dumps({"enable_support": "1", "support_type": "tree(auto)"})

        out = apply_process_overrides(after_carry, {"support_type": "normal(auto)"})

        assert json.loads(out)["support_type"] == "normal(auto)"
        assert json.loads(out)["enable_support"] == "1", "an untouched key keeps the carried value"

    def test_a_bool_is_stored_the_way_a_preset_stores_it(self):
        """⚠️ A process JSON spells booleans "1"/"0", never "True"/"False" — and
        bool is a subclass of int, so the check has to come first."""
        out = json.loads(apply_process_overrides("{}", {"enable_support": True, "spiral_mode": False}))

        assert out["enable_support"] == "1"
        assert out["spiral_mode"] == "0"

    def test_a_per_extruder_vector_keeps_its_shape(self):
        out = json.loads(apply_process_overrides("{}", {"nozzle_temperature": [220, 240]}))

        assert out["nozzle_temperature"] == ["220", "240"]


class TestABadOverrideNeverFailsTheSlice:
    def test_an_unusable_key_is_dropped_not_fatal(self):
        """⚠️ The user's other settings are still worth applying, and a hard
        failure here surfaces as "slicing failed" with no clue which field."""
        out = json.loads(apply_process_overrides("{}", {"Layer Height": 0.2, "layer_height": 0.28}))

        assert out == {"layer_height": "0.28"}

    def test_an_unusable_value_is_dropped(self):
        out = json.loads(apply_process_overrides("{}", {"weird": {"nested": 1}, "layer_height": 0.28}))

        assert out == {"layer_height": "0.28"}

    def test_an_unparseable_preset_degrades_to_the_preset(self):
        original = "{not json"

        assert apply_process_overrides(original, {"layer_height": 0.28}) == original

    def test_no_overrides_returns_the_input_untouched(self):
        original = json.dumps({"layer_height": "0.2"})

        assert apply_process_overrides(original, {}) == original
