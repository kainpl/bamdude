"""Unit tests for the shared tri-state calibration-mode type + helpers."""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from backend.app.schemas.calibration_mode import (
    CalibrationMode,
    coerce_calibration_mode,
    derive_mode,
    mode_to_bool,
)


class _Model(BaseModel):
    m: CalibrationMode = "on"


class _OptModel(BaseModel):
    m: CalibrationMode | None = None


class TestCoerce:
    def test_legacy_bool_coerces(self):
        assert coerce_calibration_mode(True) == "on"
        assert coerce_calibration_mode(False) == "off"

    def test_strings_normalised(self):
        assert coerce_calibration_mode("AUTO") == "auto"
        assert coerce_calibration_mode(" Off ") == "off"
        assert coerce_calibration_mode("on") == "on"

    def test_passthrough_none_and_other(self):
        assert coerce_calibration_mode(None) is None
        assert coerce_calibration_mode(5) == 5  # invalid — surfaces downstream


class TestField:
    def test_accepts_legacy_bool(self):
        # Old clients send a JSON bool — must keep working.
        assert _Model(m=True).m == "on"
        assert _Model(m=False).m == "off"

    @pytest.mark.parametrize("value", ["off", "auto", "on"])
    def test_accepts_tristate_strings(self, value):
        assert _Model(m=value).m == value

    def test_rejects_garbage(self):
        with pytest.raises(ValidationError):
            _Model(m="sometimes")

    def test_optional_field_keeps_none(self):
        # None means "don't change" for PATCH schemas — must not coerce to a value.
        assert _OptModel().m is None
        assert _OptModel(m=None).m is None
        assert _OptModel(m=True).m == "on"


class TestModeToBool:
    @pytest.mark.parametrize(
        ("mode", "expected"),
        [("on", True), ("off", False), ("auto", False), (True, True), (False, False), (None, False)],
    )
    def test_only_on_is_true(self, mode, expected):
        # 'auto' mirrors to False (BS: task_bed_leveling = getValue=='on').
        assert mode_to_bool(mode) is expected


class TestDeriveMode:
    def test_explicit_mode_wins(self):
        assert derive_mode("auto", legacy_bool=False) == "auto"
        assert derive_mode("off", legacy_bool=True) == "off"
        assert derive_mode("on", legacy_bool=False) == "on"

    def test_null_or_blank_derives_from_bool(self):
        assert derive_mode(None, legacy_bool=True) == "on"
        assert derive_mode(None, legacy_bool=False) == "off"
        assert derive_mode("", legacy_bool=True) == "on"
        assert derive_mode("garbage", legacy_bool=False) == "off"
