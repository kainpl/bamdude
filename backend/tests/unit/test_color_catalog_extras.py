"""Regression tests for D.15 backend completion (m076 / upstream Bambuddy #1340).

The ``color_catalog`` table gains ``extra_colors`` + ``effect_type``
columns so a catalog entry can carry the full preset look (gradient
stops + visual effect), not just a flat hex. The Spool Form's catalog-
swatch click reads these fields via the FE wiring landed earlier in
D.15 and writes them onto the spool via ``selectColor`` (#1340 /
``b51ef334``).

The FE-only port was no-op before this migration. These tests pin the
backend half: the model + Pydantic schemas round-trip the fields, and
the columns exist on a fresh ``Base.metadata.create_all`` (covers
new installs; ``m076`` handles existing installs separately).
"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from backend.app.api.routes.inventory import (
    ColorEntryCreate,
    ColorEntryResponse,
    ColorEntryUpdate,
)
from backend.app.models.color_catalog import ColorCatalogEntry


class TestColorEntrySchemaRoundtrips:
    def test_create_accepts_extra_colors_and_effect_type(self):
        entry = ColorEntryCreate(
            manufacturer="Bambu Lab",
            color_name="Galaxy Purple",
            hex_color="#1a1a2e",
            material="PLA",
            extra_colors="ec984c,6cd4bc,a66eb9",
            effect_type="sparkle",
        )
        assert entry.extra_colors == "ec984c,6cd4bc,a66eb9"
        assert entry.effect_type == "sparkle"

    def test_create_omits_optional_fields(self):
        entry = ColorEntryCreate(
            manufacturer="Generic",
            color_name="Black",
            hex_color="#000000",
        )
        assert entry.extra_colors is None
        assert entry.effect_type is None

    def test_update_accepts_extra_colors_and_effect_type(self):
        upd = ColorEntryUpdate(
            manufacturer="Bambu Lab",
            color_name="Galaxy Purple",
            hex_color="#1a1a2e",
            extra_colors="ec984c,6cd4bc",
            effect_type="gradient",
        )
        assert upd.extra_colors == "ec984c,6cd4bc"
        assert upd.effect_type == "gradient"

    def test_response_carries_extra_colors_and_effect_type(self):
        """``ColorEntryResponse`` (returned from add/update routes) must
        surface the fields so the FE catalog picker can read them off the
        catalog list at render time."""
        response = ColorEntryResponse.model_validate(
            {
                "id": 42,
                "manufacturer": "Bambu Lab",
                "color_name": "Sparkle Black",
                "hex_color": "#000000",
                "material": "PLA",
                "is_default": False,
                "extra_colors": "ec984c",
                "effect_type": "sparkle",
            }
        )
        assert response.extra_colors == "ec984c"
        assert response.effect_type == "sparkle"

    def test_extra_colors_max_length_255_enforced(self):
        """The DB column is ``VARCHAR(255)``; the schema must surface a
        clean 422 instead of letting a SQLAlchemy column-length error
        bubble up at commit time."""
        try:
            ColorEntryCreate(
                manufacturer="x",
                color_name="x",
                hex_color="#000000",
                extra_colors="a" * 256,
            )
            raise AssertionError("Expected ValidationError for >255 chars")
        except Exception as exc:
            assert "max_length" in str(exc) or "string_too_long" in str(exc)

    def test_effect_type_max_length_20_enforced(self):
        try:
            ColorEntryCreate(
                manufacturer="x",
                color_name="x",
                hex_color="#000000",
                effect_type="x" * 21,
            )
            raise AssertionError("Expected ValidationError for >20 chars")
        except Exception as exc:
            assert "max_length" in str(exc) or "string_too_long" in str(exc)


class TestColorCatalogEntryORMRoundtrips:
    @pytest.mark.asyncio
    async def test_orm_persists_extra_colors_and_effect_type(self, db_session):
        """The model fields actually round-trip through the in-memory DB
        the test fixture spins up via ``Base.metadata.create_all`` — proves
        the model declaration is wired to the table correctly."""
        row = ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Galaxy Sparkle",
            hex_color="#000000",
            material="PLA",
            is_default=False,
            extra_colors="ec984c,6cd4bc,a66eb9",
            effect_type="sparkle",
        )
        db_session.add(row)
        await db_session.commit()
        result = await db_session.execute(
            select(ColorCatalogEntry).where(ColorCatalogEntry.color_name == "Galaxy Sparkle")
        )
        stored = result.scalar_one()
        assert stored.extra_colors == "ec984c,6cd4bc,a66eb9"
        assert stored.effect_type == "sparkle"

    @pytest.mark.asyncio
    async def test_orm_defaults_extras_to_null(self, db_session):
        """Existing catalog rows (or admin imports that don't set the
        fields) default to NULL — they render as flat hex unchanged."""
        row = ColorCatalogEntry(
            manufacturer="Bambu Lab",
            color_name="Plain Red",
            hex_color="#FF0000",
            material="PLA",
            is_default=False,
        )
        db_session.add(row)
        await db_session.commit()
        result = await db_session.execute(select(ColorCatalogEntry).where(ColorCatalogEntry.color_name == "Plain Red"))
        stored = result.scalar_one()
        assert stored.extra_colors is None
        assert stored.effect_type is None
