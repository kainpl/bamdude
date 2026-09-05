"""Integration tests for Archives API endpoints.

Tests the full request/response cycle for /api/v1/archives/ endpoints.
"""

import pytest
from httpx import AsyncClient

from backend.app.services import part_stock


class TestArchivesAPI:
    """Integration tests for /api/v1/archives/ endpoints."""

    # ========================================================================
    # List endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_archives_empty(self, async_client: AsyncClient):
        """Verify empty list is returned when no archives exist."""
        response = await async_client.get("/api/v1/archives/")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert isinstance(data["data"], list)
        assert len(data["data"]) == 0
        assert data["meta"]["total"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_archives_with_data(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify list returns existing archives."""
        printer = await printer_factory()
        await archive_factory(printer.id, print_name="Test Archive")

        response = await async_client.get("/api/v1/archives/")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert len(data["data"]) >= 1
        assert any(a["print_name"] == "Test Archive" for a in data["data"])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_archives_pagination(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify pagination works correctly."""
        printer = await printer_factory()
        # Create 5 archives
        for i in range(5):
            await archive_factory(printer.id, print_name=f"Archive {i}")

        # Get first page with per_page 2
        response = await async_client.get("/api/v1/archives/?per_page=2&page=1")

        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, dict)
        assert len(data["data"]) == 2
        assert data["meta"]["total"] == 5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_archives_filter_by_printer(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify filtering by printer_id works."""
        printer1 = await printer_factory(name="Printer 1", serial_number="00M09A000000001")
        printer2 = await printer_factory(name="Printer 2", serial_number="00M09A000000002")
        await archive_factory(printer1.id, print_name="Printer 1 Archive")
        await archive_factory(printer2.id, print_name="Printer 2 Archive")

        response = await async_client.get(f"/api/v1/archives/?printer_id={printer1.id}")

        assert response.status_code == 200
        data = response.json()
        assert all(a["printer_id"] == printer1.id for a in data["data"])

    # ========================================================================
    # Get single endpoint
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_archive(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify single archive can be retrieved."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id, print_name="Get Test Archive")

        response = await async_client.get(f"/api/v1/archives/{archive.id}")

        assert response.status_code == 200
        result = response.json()
        assert result["id"] == archive.id
        assert result["print_name"] == "Get Test Archive"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_archive_not_found(self, async_client: AsyncClient):
        """Verify 404 for non-existent archive."""
        response = await async_client.get("/api/v1/archives/9999")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_response_carries_library_file_id(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """An archive dispatched from a library file exposes that file's id.

        The archive UI links a print card back to the file's print history
        with ``?file=<library_file_id>``; without the field on the response
        the link cannot be built. An archive with no source file keeps null.
        """
        from backend.app.models.library import LibraryFile

        lib = LibraryFile(
            filename="linked.3mf",
            file_path="/library/linked.3mf",
            file_type="3mf",
            file_size=42,
            file_hash="a" * 64,
        )
        db_session.add(lib)
        await db_session.commit()
        await db_session.refresh(lib)

        printer = await printer_factory()
        linked = await archive_factory(printer.id, print_name="From library", library_file_id=lib.id)
        unlinked = await archive_factory(printer.id, print_name="External print")

        detail = await async_client.get(f"/api/v1/archives/{linked.id}")
        assert detail.status_code == 200
        assert detail.json()["library_file_id"] == lib.id

        listing = await async_client.get("/api/v1/archives/")
        assert listing.status_code == 200
        by_id = {a["id"]: a for a in listing.json()["data"]}
        assert by_id[linked.id]["library_file_id"] == lib.id
        assert by_id[unlinked.id]["library_file_id"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_the_response_carries_the_skip_support_and_the_object_count(
        self, async_client: AsyncClient, archive_factory, printer_factory
    ):
        """Both were dead on the wire, and one hid the other.

        ``archive_to_response`` answers with a DICT, so a field it does not set
        is the schema's default on every response — ``skip_objects_supported``
        was False for every archive however the column read, and
        ``object_count`` was always null. The list draws the skip badge only
        inside the object-count line, so the badge could never appear at all.
        """
        printer = await printer_factory()
        skippable = await archive_factory(
            printer.id,
            print_name="Skippable",
            skip_objects_supported=True,
            extra_data={"printable_objects": {"11": "shade", "12": "arm"}},
        )
        plain = await archive_factory(printer.id, print_name="Plain")

        detail = await async_client.get(f"/api/v1/archives/{skippable.id}")
        assert detail.status_code == 200
        assert detail.json()["skip_objects_supported"] is True
        assert detail.json()["object_count"] == 2

        listing = await async_client.get("/api/v1/archives/")
        by_id = {a["id"]: a for a in listing.json()["data"]}
        assert by_id[skippable.id]["skip_objects_supported"] is True
        assert by_id[skippable.id]["object_count"] == 2
        # No metadata is "unknown", not "zero objects": the list still renders
        # the line for a hand-typed defective count on such a row.
        assert by_id[plain.id]["skip_objects_supported"] is False
        assert by_id[plain.id]["object_count"] is None

    # ========================================================================
    # Update endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_archive_name(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify archive name can be updated."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id, print_name="Original Name")

        response = await async_client.patch(f"/api/v1/archives/{archive.id}", json={"print_name": "Updated Name"})

        assert response.status_code == 200
        assert response.json()["print_name"] == "Updated Name"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_archive_notes(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify archive notes can be updated."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.patch(f"/api/v1/archives/{archive.id}", json={"notes": "Great print!"})

        assert response.status_code == 200
        assert response.json()["notes"] == "Great print!"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_archive_favorite(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify archive favorite status can be updated."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.patch(f"/api/v1/archives/{archive.id}", json={"is_favorite": True})

        assert response.status_code == 200
        assert response.json()["is_favorite"] is True

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_archive_external_url(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify archive external_url can be updated."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.patch(
            f"/api/v1/archives/{archive.id}", json={"external_url": "https://printables.com/model/12345"}
        )

        assert response.status_code == 200
        assert response.json()["external_url"] == "https://printables.com/model/12345"

        # Verify it can be cleared
        response = await async_client.patch(f"/api/v1/archives/{archive.id}", json={"external_url": None})

        assert response.status_code == 200
        assert response.json()["external_url"] is None

    # ========================================================================
    # Delete endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_archive(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify archive can be deleted."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)
        archive_id = archive.id

        response = await async_client.delete(f"/api/v1/archives/{archive_id}")

        assert response.status_code == 200

        # Verify deleted
        response = await async_client.get(f"/api/v1/archives/{archive_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_nonexistent_archive(self, async_client: AsyncClient):
        """Verify deleting non-existent archive returns 404."""
        response = await async_client.delete("/api/v1/archives/9999")

        assert response.status_code == 404

    # ========================================================================
    # Statistics endpoints
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_archive_stats(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify archive statistics can be retrieved."""
        printer = await printer_factory()
        await archive_factory(
            printer.id,
            status="completed",
            print_time_seconds=3600,
            filament_used_grams=50.0,
        )
        await archive_factory(
            printer.id,
            status="completed",
            print_time_seconds=7200,
            filament_used_grams=100.0,
        )

        response = await async_client.get("/api/v1/archives/stats")

        assert response.status_code == 200
        result = response.json()
        # Check for actual stats fields
        assert "total_prints" in result
        assert "successful_prints" in result


class TestArchivesSlimAPI:
    """Integration tests for /api/v1/archives/slim endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_empty(self, async_client: AsyncClient):
        """Verify empty list when no archives exist."""
        response = await async_client.get("/api/v1/archives/slim")

        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_returns_only_expected_fields(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify response contains only slim fields, not full archive data."""
        printer = await printer_factory()
        await archive_factory(
            printer.id,
            print_name="Slim Test",
            status="completed",
            filament_type="PLA",
            filament_color="#FF0000",
            filament_used_grams=50.0,
            print_time_seconds=3600,
            cost=1.50,
            quantity=2,
        )

        response = await async_client.get("/api/v1/archives/slim")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        item = data[0]

        # Expected fields present
        assert item["printer_id"] == printer.id
        assert item["print_name"] == "Slim Test"
        assert item["status"] == "completed"
        assert item["filament_type"] == "PLA"
        assert item["filament_color"] == "#FF0000"
        assert item["filament_used_grams"] == 50.0
        assert item["print_time_seconds"] == 3600
        assert item["cost"] == 1.50
        assert item["quantity"] == 2
        assert "created_at" in item

        # Full archive fields must NOT be present
        assert "file_path" not in item
        assert "file_size" not in item
        assert "extra_data" not in item
        assert "notes" not in item
        assert "tags" not in item
        assert "photos" not in item
        assert "content_hash" not in item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_carries_measured_energy(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """The "Most Expensive" record is computed client-side from this payload.

        ``cost`` is filament ONLY — usage_tracker fills it from grams x the price
        of each spool, plus the untracked remainder at the default rate. Ranking
        on it alone answered a narrower question than the label promises, and it
        could not be widened from the frontend because the electricity simply was
        not in this response.
        """
        printer = await printer_factory()
        await archive_factory(
            printer.id,
            print_name="Metered",
            status="completed",
            cost=1.50,
            energy_kwh=0.9,
            energy_cost=0.42,
        )

        item = (await async_client.get("/api/v1/archives/slim")).json()[0]
        assert item["cost"] == 1.50
        assert item["energy_kwh"] == 0.9
        assert item["energy_cost"] == 0.42

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_reports_no_energy_as_null_not_zero(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """A printer with no smart plug drew *something*; we just did not measure it.

        NULL says that. A zero would claim the print ran on no electricity, and
        would be indistinguishable from a measured zero — which is exactly the
        distinction the plug drivers already refuse to blur.
        """
        printer = await printer_factory()
        await archive_factory(printer.id, print_name="Unmetered", status="completed", cost=1.50)

        item = (await async_client.get("/api/v1/archives/slim")).json()[0]
        assert item["energy_kwh"] is None
        assert item["energy_cost"] is None
        assert "duplicates" not in item
        assert "duplicate_count" not in item

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_computes_actual_time(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify actual_time_seconds is computed from started_at/completed_at."""
        from datetime import datetime, timezone

        printer = await printer_factory()
        started = datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        completed = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)  # 2 hours = 7200s
        await archive_factory(
            printer.id,
            status="completed",
            started_at=started,
            completed_at=completed,
        )

        response = await async_client.get("/api/v1/archives/slim")

        assert response.status_code == 200
        item = response.json()[0]
        assert item["actual_time_seconds"] == 7200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_actual_time_null_for_failed(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify actual_time_seconds is null for non-completed prints."""
        from datetime import datetime, timezone

        printer = await printer_factory()
        await archive_factory(
            printer.id,
            status="failed",
            started_at=datetime(2024, 1, 1, 10, 0, 0, tzinfo=timezone.utc),
            completed_at=datetime(2024, 1, 1, 11, 0, 0, tzinfo=timezone.utc),
        )

        response = await async_client.get("/api/v1/archives/slim")

        assert response.status_code == 200
        item = response.json()[0]
        assert item["actual_time_seconds"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_date_filtering(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify date_from and date_to filters work."""
        from datetime import datetime, timezone

        printer = await printer_factory()
        await archive_factory(
            printer.id,
            print_name="Old Print",
            created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
        await archive_factory(
            printer.id,
            print_name="New Print",
            created_at=datetime(2024, 6, 15, tzinfo=timezone.utc),
        )

        # Filter to only June 2024
        response = await async_client.get("/api/v1/archives/slim?date_from=2024-06-01&date_to=2024-06-30")

        assert response.status_code == 200
        data = response.json()
        assert len(data) == 1
        assert data[0]["print_name"] == "New Print"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_pagination(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify limit and offset work."""
        printer = await printer_factory()
        for i in range(5):
            await archive_factory(printer.id, print_name=f"Print {i}")

        response = await async_client.get("/api/v1/archives/slim?limit=2&offset=0")

        assert response.status_code == 200
        assert len(response.json()) == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_slim_excludes_trashed(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Trashed (soft-deleted) archives are excluded from /slim so the
        dashboard/stats widgets it feeds agree with Quick Stats (E.9)."""
        from datetime import datetime, timezone

        printer = await printer_factory()
        await archive_factory(printer.id, print_name="Live Print")
        await archive_factory(printer.id, print_name="Trashed Print", deleted_at=datetime.now(timezone.utc))

        response = await async_client.get("/api/v1/archives/slim")
        assert response.status_code == 200
        names = [a["print_name"] for a in response.json()]
        assert "Live Print" in names
        assert "Trashed Print" not in names


class TestArchiveDataIntegrity:
    """Tests for archive data integrity."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_linked_to_printer(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify archive is properly linked to printer."""
        printer = await printer_factory(name="My Printer")
        archive = await archive_factory(printer.id)

        response = await async_client.get(f"/api/v1/archives/{archive.id}")

        assert response.status_code == 200
        result = response.json()
        assert result["printer_id"] == printer.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_stores_print_data(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify archive stores all print data correctly."""
        printer = await printer_factory()
        archive = await archive_factory(
            printer.id,
            print_name="Test Print",
            filename="test.3mf",
            status="completed",
            filament_type="PLA",
            filament_used_grams=75.5,
            print_time_seconds=5400,
        )

        response = await async_client.get(f"/api/v1/archives/{archive.id}")

        assert response.status_code == 200
        result = response.json()
        assert result["print_name"] == "Test Print"
        assert result["filename"] == "test.3mf"
        assert result["status"] == "completed"
        assert result["filament_type"] == "PLA"
        assert result["filament_used_grams"] == 75.5
        assert result["print_time_seconds"] == 5400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_update_persists(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """CRITICAL: Verify archive updates persist."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id, notes="Original notes")

        # Update
        await async_client.patch(f"/api/v1/archives/{archive.id}", json={"notes": "Updated notes", "is_favorite": True})

        # Verify persistence
        response = await async_client.get(f"/api/v1/archives/{archive.id}")
        result = response.json()
        assert result["notes"] == "Updated notes"
        assert result["is_favorite"] is True


class TestArchiveF3DEndpoints:
    """Tests for F3D (Fusion 360 design file) attachment endpoints."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_response_includes_f3d_path(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify f3d_path is included in archive response."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id, f3d_path="archives/test/design.f3d")

        response = await async_client.get(f"/api/v1/archives/{archive.id}")

        assert response.status_code == 200
        result = response.json()
        assert "f3d_path" in result
        assert result["f3d_path"] == "archives/test/design.f3d"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_archive_response_f3d_path_null_when_not_set(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify f3d_path is null when no F3D file attached."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.get(f"/api/v1/archives/{archive.id}")

        assert response.status_code == 200
        result = response.json()
        assert "f3d_path" in result
        assert result["f3d_path"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_upload_f3d_to_nonexistent_archive(self, async_client: AsyncClient):
        """Verify 404 when uploading F3D to non-existent archive."""
        # Create a minimal file-like upload
        files = {"file": ("design.f3d", b"fake f3d content", "application/octet-stream")}
        response = await async_client.post("/api/v1/archives/9999/f3d", files=files)

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_download_f3d_not_found_when_no_file(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify 404 when downloading F3D from archive without F3D file."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.get(f"/api/v1/archives/{archive.id}/f3d")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_download_f3d_nonexistent_archive(self, async_client: AsyncClient):
        """Verify 404 when downloading F3D from non-existent archive."""
        response = await async_client.get("/api/v1/archives/9999/f3d")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_f3d_nonexistent_archive(self, async_client: AsyncClient):
        """Verify 404 when deleting F3D from non-existent archive."""
        response = await async_client.delete("/api/v1/archives/9999/f3d")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_f3d_when_no_file(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify 404 when deleting F3D from archive without F3D file."""
        printer = await printer_factory()
        archive = await archive_factory(printer.id)

        response = await async_client.delete(f"/api/v1/archives/{archive.id}/f3d")

        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_archives_includes_f3d_path(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify f3d_path is included in archive list responses."""
        printer = await printer_factory()
        await archive_factory(printer.id, print_name="With F3D", f3d_path="archives/test/design.f3d")
        await archive_factory(printer.id, print_name="Without F3D")

        response = await async_client.get("/api/v1/archives/")

        assert response.status_code == 200
        data = response.json()["data"]
        assert len(data) >= 2

        with_f3d = next((a for a in data if a["print_name"] == "With F3D"), None)
        without_f3d = next((a for a in data if a["print_name"] == "Without F3D"), None)

        assert with_f3d is not None
        assert with_f3d["f3d_path"] == "archives/test/design.f3d"
        assert without_f3d is not None
        assert without_f3d["f3d_path"] is None

    # ========================================================================
    # Multi-Plate 3MF endpoints (Issue #93)
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_archive_plates_not_found(self, async_client: AsyncClient):
        """Verify 404 when fetching plates for non-existent archive."""
        response = await async_client.get("/api/v1/archives/999999/plates")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_plate_thumbnail_not_found(self, async_client: AsyncClient):
        """Verify 404 when fetching plate thumbnail for non-existent archive."""
        response = await async_client.get("/api/v1/archives/999999/plate-thumbnail/1")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_filament_requirements_not_found(self, async_client: AsyncClient):
        """Verify filament-requirements returns 404 for non-existent archive."""
        response = await async_client.get("/api/v1/archives/999999/filament-requirements")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_filament_requirements_with_plate_id_not_found(self, async_client: AsyncClient):
        """Verify filament-requirements with plate_id returns 404 for non-existent archive."""
        response = await async_client.get("/api/v1/archives/999999/filament-requirements?plate_id=1")
        assert response.status_code == 404

    # ========================================================================
    # Tag Management endpoints (Issue #183)
    # ========================================================================

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_tags_empty(self, async_client: AsyncClient):
        """Verify empty list when no tags exist."""
        response = await async_client.get("/api/v1/archives/tags")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_tags_with_data(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify tags are returned with counts."""
        printer = await printer_factory()
        await archive_factory(printer.id, print_name="Archive 1", tags="functional, test")
        await archive_factory(printer.id, print_name="Archive 2", tags="functional, calibration")
        await archive_factory(printer.id, print_name="Archive 3", tags="test")

        response = await async_client.get("/api/v1/archives/tags")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)

        # Convert to dict for easier lookup
        tags_dict = {t["name"]: t["count"] for t in data}
        assert tags_dict.get("functional") == 2
        assert tags_dict.get("test") == 2
        assert tags_dict.get("calibration") == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_tags_sorted_by_count(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        """Verify tags are sorted by count descending, then by name."""
        printer = await printer_factory()
        await archive_factory(printer.id, tags="alpha")
        await archive_factory(printer.id, tags="beta, alpha")
        await archive_factory(printer.id, tags="gamma, beta, alpha")

        response = await async_client.get("/api/v1/archives/tags")
        assert response.status_code == 200
        data = response.json()

        # alpha=3, beta=2, gamma=1
        assert data[0]["name"] == "alpha"
        assert data[0]["count"] == 3
        assert data[1]["name"] == "beta"
        assert data[1]["count"] == 2
        assert data[2]["name"] == "gamma"
        assert data[2]["count"] == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rename_tag(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify renaming a tag updates all archives."""
        printer = await printer_factory()
        a1 = await archive_factory(printer.id, print_name="Archive 1", tags="old-tag, other")
        a2 = await archive_factory(printer.id, print_name="Archive 2", tags="old-tag")
        await archive_factory(printer.id, print_name="Archive 3", tags="different")

        response = await async_client.put("/api/v1/archives/tags/old-tag", json={"new_name": "new-tag"})
        assert response.status_code == 200
        data = response.json()
        assert data["affected"] == 2

        # Verify the archives were updated
        response = await async_client.get(f"/api/v1/archives/{a1.id}")
        assert "new-tag" in response.json()["tags"]
        assert "old-tag" not in response.json()["tags"]

        response = await async_client.get(f"/api/v1/archives/{a2.id}")
        assert response.json()["tags"] == "new-tag"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rename_tag_no_change(self, async_client: AsyncClient):
        """Verify renaming to same name returns 0 affected."""
        response = await async_client.put("/api/v1/archives/tags/some-tag", json={"new_name": "some-tag"})
        assert response.status_code == 200
        assert response.json()["affected"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rename_tag_empty_name_error(self, async_client: AsyncClient):
        """Verify renaming to empty name returns error."""
        response = await async_client.put("/api/v1/archives/tags/some-tag", json={"new_name": ""})
        assert response.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_tag(self, async_client: AsyncClient, archive_factory, printer_factory, db_session):
        """Verify deleting a tag removes it from all archives."""
        printer = await printer_factory()
        a1 = await archive_factory(printer.id, print_name="Archive 1", tags="delete-me, keep")
        a2 = await archive_factory(printer.id, print_name="Archive 2", tags="delete-me")
        await archive_factory(printer.id, print_name="Archive 3", tags="different")

        response = await async_client.delete("/api/v1/archives/tags/delete-me")
        assert response.status_code == 200
        data = response.json()
        assert data["affected"] == 2

        # Verify the archives were updated
        response = await async_client.get(f"/api/v1/archives/{a1.id}")
        assert response.json()["tags"] == "keep"

        response = await async_client.get(f"/api/v1/archives/{a2.id}")
        # Should be None or empty when last tag is removed
        assert response.json()["tags"] is None or response.json()["tags"] == ""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_tag_not_found(self, async_client: AsyncClient):
        """Verify deleting non-existent tag returns 0 affected."""
        response = await async_client.delete("/api/v1/archives/tags/nonexistent-tag")
        assert response.status_code == 200
        assert response.json()["affected"] == 0


class TestNo3MFWarning:
    """`GET /archives/no-3mf-warning` — install step 4 reactive nudge.

    The connection diagnostic's external_storage check only catches the
    printer-side variant of the setting (newer firmware). For older slicers
    where the toggle lives only in the slicer, the printer never reports it.
    The fallback path creates the archive with extra_data.no_3mf_available=True;
    this endpoint exposes that as a boolean so the frontend can surface a
    one-time banner.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_true_when_recent_fallback_exists(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory()
        await archive_factory(printer.id, extra_data={"no_3mf_available": True})

        response = await async_client.get("/api/v1/archives/no-3mf-warning")

        assert response.status_code == 200
        assert response.json() == {"has_fallback": True}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_false_when_no_archives(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/archives/no-3mf-warning")

        assert response.status_code == 200
        assert response.json() == {"has_fallback": False}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_returns_false_when_only_normal_archives(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory()
        # extra_data has other keys but no_3mf_available is absent — normal
        # archives must not trigger the nudge.
        await archive_factory(printer.id, extra_data={"makerworld_url": "https://example"})
        await archive_factory(printer.id, extra_data=None)

        response = await async_client.get("/api/v1/archives/no-3mf-warning")

        assert response.status_code == 200
        assert response.json() == {"has_fallback": False}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ignores_archives_older_than_30_days(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        from datetime import datetime, timedelta, timezone

        from backend.app.models.archive import PrintArchive

        printer = await printer_factory()
        archive = await archive_factory(printer.id, extra_data={"no_3mf_available": True})
        # Backdate past the 30-day window — old fallbacks are forgiven.
        archive.created_at = datetime.now(timezone.utc) - timedelta(days=45)
        await db_session.commit()

        response = await async_client.get("/api/v1/archives/no-3mf-warning")

        assert response.status_code == 200
        assert response.json() == {"has_fallback": False}
        # Sanity: row really is in the DB, we just don't surface it.
        assert (await db_session.get(PrintArchive, archive.id)) is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_ignores_soft_deleted_fallbacks(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        from datetime import datetime, timezone

        printer = await printer_factory()
        archive = await archive_factory(printer.id, extra_data={"no_3mf_available": True})
        archive.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        response = await async_client.get("/api/v1/archives/no-3mf-warning")

        assert response.status_code == 200
        # Soft-deleted fallbacks have been actioned. Stop nudging.
        assert response.json() == {"has_fallback": False}


class TestArchiveDeleteImpact:
    """Delete-impact pre-flight + mid-print 409 guard + pending cancel (#1734)."""

    async def _make_queue_with_items(self, db_session, archive_id, printer_id, statuses):
        """Create one PrinterQueue + one PrintQueueItem per status, single commit."""
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_queue import PrinterQueue

        queue = PrinterQueue(printer_id=printer_id)
        db_session.add(queue)
        await db_session.flush()
        items = []
        for pos, status in enumerate(statuses, start=1):
            item = PrintQueueItem(queue_id=queue.id, archive_id=archive_id, status=status, position=pos)
            db_session.add(item)
            items.append(item)
        await db_session.commit()
        return items

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_impact_counts_related_and_printing(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory()
        archive = await archive_factory(printer.id, print_name="Impact")
        await self._make_queue_with_items(db_session, archive.id, printer.id, ["pending", "printing"])

        resp = await async_client.get(f"/api/v1/archives/{archive.id}/delete-impact")
        assert resp.status_code == 200
        data = resp.json()
        assert data["related_queue_items"] == 2
        assert data["currently_printing"] == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_blocked_while_printing(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        printer = await printer_factory()
        archive = await archive_factory(printer.id, print_name="Busy")
        await self._make_queue_with_items(db_session, archive.id, printer.id, ["printing"])

        resp = await async_client.delete(f"/api/v1/archives/{archive.id}")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_cancels_pending_related(
        self, async_client: AsyncClient, archive_factory, printer_factory, db_session
    ):
        from backend.app.models.print_queue import PrintQueueItem

        printer = await printer_factory()
        archive = await archive_factory(printer.id, print_name="Trash")
        (item,) = await self._make_queue_with_items(db_session, archive.id, printer.id, ["pending"])
        item_id = item.id

        resp = await async_client.delete(f"/api/v1/archives/{archive.id}")
        assert resp.status_code == 200
        assert resp.json()["trashed"] is True

        db_session.expire_all()
        refreshed = await db_session.get(PrintQueueItem, item_id)
        assert refreshed.status == "cancelled"


class TestArchiveSortByPrinter:
    """`?sort_by=printer-asc|printer-desc`, added so the list view's Printer
    column can be clicked like the others.

    Ordering by printer is the only sort that has to leave the archive table to
    answer, which is where the two failure modes live: dropping the rows that
    have no printer, and ordering by an id instead of by the name on screen.
    """

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_orders_by_printer_name_not_by_id(self, async_client: AsyncClient, archive_factory, printer_factory):
        # Ids ascend while names descend, so an id-ordered list is the exact
        # reverse of a correct one — nothing subtle to miss.
        first = await printer_factory(name="Zulu")
        second = await printer_factory(name="Alpha")
        await archive_factory(first.id, print_name="on-zulu")
        await archive_factory(second.id, print_name="on-alpha")

        response = await async_client.get("/api/v1/archives/?sort_by=printer-asc")

        assert response.status_code == 200
        assert [a["print_name"] for a in response.json()["data"]] == ["on-alpha", "on-zulu"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_reverses_on_descending(self, async_client: AsyncClient, archive_factory, printer_factory):
        # Three, created in an order that is neither the ascending nor the
        # descending answer. Two rows would let this pass on the insertion
        # order alone: the archives share a created_at to the second, so the
        # date fallback has no tiebreaker and can return either arrangement.
        mike = await printer_factory(name="Mike")
        zulu = await printer_factory(name="Zulu")
        alpha = await printer_factory(name="Alpha")
        await archive_factory(mike.id, print_name="on-mike")
        await archive_factory(zulu.id, print_name="on-zulu")
        await archive_factory(alpha.id, print_name="on-alpha")

        response = await async_client.get("/api/v1/archives/?sort_by=printer-desc")

        assert [a["print_name"] for a in response.json()["data"]] == ["on-zulu", "on-mike", "on-alpha"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_keeps_archives_that_have_no_printer(
        self, async_client: AsyncClient, archive_factory, printer_factory
    ):
        """An external print, or one whose printer was deleted, still has to be
        in a list that is merely being re-ordered — a join would silently drop
        it, and the row count is the only thing that would show it."""
        printer = await printer_factory(name="Alpha")
        await archive_factory(printer.id, print_name="on-alpha")
        await archive_factory(None, print_name="orphan", sliced_for_model="P1S")

        response = await async_client.get("/api/v1/archives/?sort_by=printer-asc")

        names = [a["print_name"] for a in response.json()["data"]]
        assert sorted(names) == ["on-alpha", "orphan"]
        assert response.json()["meta"]["total"] == 2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_a_printerless_archive_sorts_by_what_it_displays(
        self, async_client: AsyncClient, archive_factory, printer_factory
    ):
        """With no printer the column shows `sliced_for_model`, so that is what
        it sorts by — otherwise those rows would all collapse to one end
        regardless of what the operator can see in them."""
        printer = await printer_factory(name="Mike")
        await archive_factory(printer.id, print_name="on-mike")
        await archive_factory(None, print_name="sliced-for-a1", sliced_for_model="A1")
        await archive_factory(None, print_name="sliced-for-x1", sliced_for_model="X1C")

        response = await async_client.get("/api/v1/archives/?sort_by=printer-asc")

        assert [a["print_name"] for a in response.json()["data"]] == [
            "sliced-for-a1",
            "on-mike",
            "sliced-for-x1",
        ]


# ============================================================================
# Free stock (pass 8, Decision 3): an order-less print's parts
# ============================================================================
#
# ⚠️ Ids are carried as plain ints, never as ORM instances. The ``get_db``
# override hands the handler THIS test's session, so the handler's commit
# expires every object the test is holding — and reading an expired attribute
# back on an async session is a lazy load with no greenlet under it.


async def _stocked_product(db_session, *, file_id: int = 900) -> tuple[int, int]:
    """A product whose whole-file plate yields lids. Returns ``(product_id, lid_id)``."""
    from backend.app.models.product import Product, ProductPart, ProductPlate

    product = Product(name="Lamp")
    db_session.add(product)
    await db_session.flush()
    lid = ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1)
    db_session.add_all([lid, ProductPlate(product_id=product.id, library_file_id=file_id, plate_index=0)])
    await db_session.flush()
    ids = (product.id, lid.id)
    await db_session.commit()
    return ids


async def _print_of(
    db_session, printer_id, archive_factory, *, file_id: int = 900, project_id=None, lids: int = 4
) -> int:
    """One completed print of that plate. Returns the archive id."""
    from backend.app.models.archive_part import PrintArchivePart

    archive = await archive_factory(
        printer_id,
        print_name="Lids",
        library_file_id=file_id,
        plate_index=1,
        project_id=project_id,
        status="completed",
    )
    archive_id = archive.id
    db_session.add(PrintArchivePart(archive_id=archive_id, name="lid", name_key="lid", quantity=lids, defective=0))
    await db_session.commit()
    return archive_id


async def _stock_rows(db_session) -> list[tuple]:
    """``(part_id, reason, delta, archive_id, note)`` per movement, oldest first."""
    from sqlalchemy import select

    from backend.app.models.part_stock import ProductPartStockMovement

    db_session.expire_all()
    rows = (
        (await db_session.execute(select(ProductPartStockMovement).order_by(ProductPartStockMovement.id)))
        .scalars()
        .all()
    )
    return [(r.product_part_id, r.reason, r.delta, r.archive_id, r.note) for r in rows]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_counting_an_old_order_less_print_into_stock(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """History is deliberately NOT backfilled — nobody knows which of last
    year's order-less prints were shipped. This is the operator vouching for
    one of them, and the only way such a print reaches the shelf."""
    _product_id, lid_id = await _stocked_product(db_session)
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)

    r = await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")

    assert r.status_code == 200, r.text
    assert r.json() == [{"part_id": lid_id, "name": "lid", "delta": 4}]
    assert await _stock_rows(db_session) == [
        (lid_id, "unfiled_print", 4, archive_id, part_stock.NOTE_COUNTED_BY_OPERATOR)
    ], "the note is a token the product page translates, never an English sentence (Ruling 17)"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_counting_the_same_print_into_stock_twice_is_refused(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """409, not a silent no-op: the operator pressed a button and is entitled to
    be told the parts are already counted rather than left to wonder."""
    await _stocked_product(db_session)
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    assert (await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")).status_code == 200

    r = await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")

    assert r.status_code == 409
    assert len(await _stock_rows(db_session)) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_print_filed_under_an_order_cannot_be_counted_into_stock(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """Its parts are counted by the order's own figures; counting them here too
    is the double count Decision 3 exists to prevent."""
    from backend.app.models.project import Project

    await _stocked_product(db_session)
    project = Project(name="O")
    db_session.add(project)
    await db_session.flush()
    project_id = project.id
    await db_session.commit()
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory, project_id=project_id)

    r = await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")

    assert r.status_code == 409
    assert await _stock_rows(db_session) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filing_a_counted_print_under_an_order_takes_its_stock_back(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """The archive editor's project change. Without the reversal the same parts
    would sit on the shelf AND count towards the order they were just filed
    under."""
    from backend.app.models.project import Project

    _product_id, lid_id = await _stocked_product(db_session)
    project = Project(name="Lamps")
    db_session.add(project)
    await db_session.flush()
    project_id = project.id
    await db_session.commit()
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")

    r = await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": project_id})

    assert r.status_code == 200, r.text
    assert await _stock_rows(db_session) == [
        (lid_id, "unfiled_print", 4, archive_id, part_stock.NOTE_COUNTED_BY_OPERATOR),
        (lid_id, "manual", -4, archive_id, part_stock.NOTE_FILED_UNDER_ORDER),
    ], "the parts are counted by the order now, not by the shelf"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filing_a_print_a_second_time_reverses_nothing_more(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """Moving the print on to another order must not take the parts back twice —
    the first filing already zeroed what this archive put on the shelf."""
    from backend.app.models.project import Project

    _product_id, lid_id = await _stocked_product(db_session)
    db_session.add_all([Project(name="Lamps"), Project(name="Sconces")])
    await db_session.commit()
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")
    await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": 1})

    r = await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": 2})

    assert r.status_code == 200, r.text
    assert [row[1] for row in await _stock_rows(db_session)] == ["unfiled_print", "manual"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filing_a_print_whose_stock_is_already_spent_still_files_it(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """The stock went out to another order between the print and the filing, so
    the credit cannot be taken back. Re-filing a print corrects the print
    HISTORY and must not be refused over bookkeeping — the ledger keeps the
    truth of what is on the shelf and the operator corrects it by hand."""
    from backend.app.models.project import Project
    from backend.app.services.part_stock import move

    _product_id, lid_id = await _stocked_product(db_session)
    project = Project(name="Lamps")
    db_session.add(project)
    await db_session.flush()
    project_id = project.id
    await db_session.commit()
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")
    await move(db_session, part_id=lid_id, delta=-4, reason="reserved_for_order")
    await db_session.commit()

    r = await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": project_id})

    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == project_id
    # 4 in, 4 reserved out, nothing reversed — and the balance never went below zero.
    assert [row[1:3] for row in await _stock_rows(db_session)] == [("unfiled_print", 4), ("reserved_for_order", -4)]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_hard_deleting_an_archive_leaves_the_parts_on_the_shelf(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """``archive_id`` is ON DELETE SET NULL and SQLite honours no such clause.
    The print history goes; the parts it made are still in a drawer."""
    from backend.app.services.archive import ArchiveService

    _product_id, lid_id = await _stocked_product(db_session)
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")

    assert await ArchiveService(db_session).delete_archive(archive_id) is True

    assert await _stock_rows(db_session) == [(lid_id, "unfiled_print", 4, None, part_stock.NOTE_COUNTED_BY_OPERATOR)]


async def _admin_id(db_session) -> int:
    """The user the test client is authenticated as (seeded in ``_build_client``)."""
    from sqlalchemy import select

    from backend.app.models.user import User

    return await db_session.scalar(select(User.id).where(User.username == "test_admin"))


@pytest.mark.asyncio
@pytest.mark.integration
async def test_counting_a_print_into_stock_records_who_asked(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """The button has an operator behind it, unlike the completion handler,
    which writes with no user (Decision 7). The product page's movements table
    shows who."""
    from sqlalchemy import select

    from backend.app.models.part_stock import ProductPartStockMovement

    await _stocked_product(db_session)
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    admin_id = await _admin_id(db_session)

    assert (await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")).status_code == 200

    db_session.expire_all()
    rows = (await db_session.execute(select(ProductPartStockMovement))).scalars().all()
    assert [r.created_by for r in rows] == [admin_id]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_taking_a_print_back_out_of_an_order_puts_its_parts_back(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """Ruling 11. Filing reversed the credit because the order counted the
    parts; un-filing has to undo that, or a plate quietly disappears every time
    somebody corrects a filing."""
    from backend.app.models.project import Project

    _product_id, lid_id = await _stocked_product(db_session)
    project = Project(name="Lamps")
    db_session.add(project)
    await db_session.flush()
    project_id = project.id
    await db_session.commit()
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")
    await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": project_id})

    r = await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": None})

    assert r.status_code == 200, r.text
    assert r.json()["project_id"] is None
    rows = await _stock_rows(db_session)
    assert [row[1:3] for row in rows] == [("unfiled_print", 4), ("manual", -4), ("unfiled_print", 4)]
    assert sum(row[2] for row in rows) == 4, "the parts are back on the shelf"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_re_credited_print_cannot_be_counted_into_stock_again(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """The 409 reads the archive's NET, the same function the writer's
    idempotency check reads — so it says "already counted" exactly while the
    parts are actually standing on the shelf, whatever the row history is."""
    from backend.app.models.project import Project

    await _stocked_product(db_session)
    project = Project(name="Lamps")
    db_session.add(project)
    await db_session.flush()
    project_id = project.id
    await db_session.commit()
    printer = await printer_factory()
    archive_id = await _print_of(db_session, printer.id, archive_factory)
    await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")
    await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": project_id})
    await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": None})

    r = await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")

    assert r.status_code == 409
    assert "already been counted" in r.json()["detail"]
    assert len(await _stock_rows(db_session)) == 3, "the refused call wrote nothing"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_filing_reverses_every_part_it_can_even_when_one_is_spent(
    async_client: AsyncClient, db_session, archive_factory, printer_factory
):
    """Ruling 12a end to end: the lid's stock has gone out to another order and
    the base's has not. The base comes back off the shelf, the archive is
    filed, and only the lid is left for the operator to correct."""
    from backend.app.models.archive_part import PrintArchivePart
    from backend.app.models.product import Product, ProductPart, ProductPlate
    from backend.app.models.project import Project
    from backend.app.services.part_stock import move

    product = Product(name="Lamp")
    project = Project(name="Lamps")
    db_session.add_all([product, project])
    await db_session.flush()
    lid = ProductPart(product_id=product.id, kind="printed", name="lid", name_key="lid", qty_per_unit=1)
    base = ProductPart(product_id=product.id, kind="printed", name="base", name_key="base", qty_per_unit=1)
    db_session.add_all([lid, base, ProductPlate(product_id=product.id, library_file_id=901, plate_index=0)])
    await db_session.flush()
    lid_id, base_id, project_id = lid.id, base.id, project.id
    archive = await archive_factory(
        (await printer_factory()).id, library_file_id=901, plate_index=1, status="completed"
    )
    archive_id = archive.id
    db_session.add_all(
        [
            PrintArchivePart(archive_id=archive_id, name="lid", name_key="lid", quantity=4),
            PrintArchivePart(archive_id=archive_id, name="base", name_key="base", quantity=2),
        ]
    )
    await db_session.commit()
    await async_client.post(f"/api/v1/archives/{archive_id}/count-into-stock")
    await move(db_session, part_id=lid_id, delta=-4, reason="reserved_for_order")
    await db_session.commit()

    r = await async_client.patch(f"/api/v1/archives/{archive_id}", json={"project_id": project_id})

    assert r.status_code == 200, r.text
    assert r.json()["project_id"] == project_id, "a refused reversal must never block the filing"
    per_part = {}
    for part_id, _reason, delta, _archive_id, _note in await _stock_rows(db_session):
        per_part[part_id] = per_part.get(part_id, 0) + delta
    assert per_part == {lid_id: 0, base_id: 0}, "the base was reversed; the lid's stock was already spent"
