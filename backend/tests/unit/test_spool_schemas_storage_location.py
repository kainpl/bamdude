"""Schema tests for the spool storage_location field (upstream Bambuddy #1291).

The ``storage_location`` column existed on the Spool ORM model but was
missing from ``SpoolBase``, ``SpoolUpdate``, and (by inheritance)
``SpoolResponse``. Pydantic silently drops unknown fields, so PATCH
writes never reached the DB and reads omitted the field — the inventory
table always showed "—" in the Storage Location column even after
saving. The fix is purely additive on the schema layer.

Spoolman mode was unaffected because it goes through a separate proxy
backend (``_spoolman_helpers``) with its own schema.
"""

from datetime import datetime, timezone

from backend.app.schemas.spool import SpoolCreate, SpoolResponse, SpoolUpdate


class TestStorageLocationRoundtrips:
    """The bug was that ``storage_location`` wasn't on the schemas at all
    — pin the round-trip so a future refactor can't quietly drop it again."""

    def test_create_accepts_storage_location(self):
        spool = SpoolCreate(material="PLA", storage_location="Drybox #1")
        assert spool.storage_location == "Drybox #1"

    def test_create_storage_location_optional(self):
        spool = SpoolCreate(material="PLA")
        assert spool.storage_location is None

    def test_update_accepts_storage_location(self):
        update = SpoolUpdate(storage_location="Top shelf")
        assert update.storage_location == "Top shelf"

    def test_update_omits_unset_storage_location(self):
        """A PATCH that doesn't mention ``storage_location`` must NOT clear
        it — ``model_dump(exclude_unset=True)`` keeps the field out of the
        update dict so the route's setattr loop skips it."""
        update = SpoolUpdate.model_validate({})
        dumped = update.model_dump(exclude_unset=True)
        assert "storage_location" not in dumped

    def test_update_explicit_null_clears_storage_location(self):
        """A PATCH that explicitly sends ``storage_location=null`` must
        reach the route's update_data dict as None, so setattr writes NULL
        to the DB — that's how the UI clears the field."""
        update = SpoolUpdate.model_validate({"storage_location": None})
        dumped = update.model_dump(exclude_unset=True)
        assert "storage_location" in dumped
        assert dumped["storage_location"] is None

    def test_response_carries_storage_location(self):
        """``SpoolResponse`` inherits from ``SpoolBase``, so the field must
        surface on read too — otherwise the inventory table silently always
        shows '-'."""
        now = datetime.now(timezone.utc)
        response = SpoolResponse.model_validate(
            {
                "id": 1,
                "material": "PLA",
                "storage_location": "Drybox #1",
                "created_at": now,
                "updated_at": now,
            }
        )
        assert response.storage_location == "Drybox #1"

    def test_max_length_255_enforced(self):
        """The DB column is ``String(255)``; the schema must surface a
        clean 422 instead of letting a SQLAlchemy column-length error
        bubble up at commit time."""
        try:
            SpoolCreate(material="PLA", storage_location="x" * 256)
            raise AssertionError("Expected ValidationError for >255 chars")
        except Exception as exc:
            assert "max_length" in str(exc) or "string_too_long" in str(exc)
