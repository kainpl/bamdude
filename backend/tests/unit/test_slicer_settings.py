"""Unit tests for preferred_slicer setting in AppSettings schema."""

import pytest

from backend.app.schemas.settings import AppSettings, AppSettingsUpdate


@pytest.mark.unit
class TestPreferredSlicerSchema:
    """Tests for the preferred_slicer field in settings schemas."""

    def test_default_value_is_bambu_studio(self):
        """Default preferred_slicer should be bambu_studio."""
        settings = AppSettings()
        assert settings.preferred_slicer == "bambu_studio"

    def test_set_to_orcaslicer(self):
        """Should accept orcaslicer as a valid value."""
        settings = AppSettings(preferred_slicer="orcaslicer")
        assert settings.preferred_slicer == "orcaslicer"

    def test_set_to_bambu_studio_explicit(self):
        """Should accept bambu_studio as an explicit value."""
        settings = AppSettings(preferred_slicer="bambu_studio")
        assert settings.preferred_slicer == "bambu_studio"

    def test_update_schema_default_is_none(self):
        """AppSettingsUpdate preferred_slicer should default to None."""
        update = AppSettingsUpdate()
        assert update.preferred_slicer is None

    def test_update_schema_accepts_value(self):
        """AppSettingsUpdate should accept a preferred_slicer value."""
        update = AppSettingsUpdate(preferred_slicer="orcaslicer")
        assert update.preferred_slicer == "orcaslicer"

    def test_serialization_roundtrip(self):
        """Settings should survive serialization roundtrip."""
        settings = AppSettings(preferred_slicer="orcaslicer")
        data = settings.model_dump()
        restored = AppSettings(**data)
        assert restored.preferred_slicer == "orcaslicer"

    def test_partial_update_preserves_other_fields(self):
        """Updating preferred_slicer should not affect other fields."""
        update = AppSettingsUpdate(preferred_slicer="orcaslicer")
        data = update.model_dump(exclude_none=True)
        assert data == {"preferred_slicer": "orcaslicer"}
