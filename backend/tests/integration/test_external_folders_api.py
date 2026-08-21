"""Integration tests for External Folder API endpoints."""

import os
import tempfile
from pathlib import Path

import pytest
from httpx import AsyncClient


class TestExternalFolderCreation:
    """Tests for POST /library/folders/external."""

    @pytest.fixture
    def external_dir(self, tmp_path):
        """Create a temporary directory to act as an external folder."""
        ext_dir = tmp_path / "nas_share"
        ext_dir.mkdir()
        # Add some test files
        (ext_dir / "benchy.3mf").write_bytes(b"fake3mf")
        (ext_dir / "bracket.stl").write_bytes(b"fakestl")
        (ext_dir / "print.gcode").write_text("G28\nG1 X10 Y10")
        (ext_dir / "readme.txt").write_text("not a print file")
        (ext_dir / ".hidden.3mf").write_bytes(b"hidden")
        return ext_dir

    @pytest.fixture
    def nested_external_dir(self, external_dir):
        """Create a nested subdirectory in the external folder."""
        sub = external_dir / "subfolder"
        sub.mkdir()
        (sub / "nested_part.stl").write_bytes(b"nestedstl")
        return external_dir

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_external_folder(self, async_client: AsyncClient, db_session, external_dir):
        """Verify external folder can be created with valid path."""
        data = {
            "name": "NAS Prints",
            "external_path": str(external_dir),
            "readonly": True,
            "show_hidden": False,
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "NAS Prints"
        assert result["is_external"] is True
        assert result["external_readonly"] is True
        assert result["external_show_hidden"] is False
        assert result["external_path"] == str(external_dir.resolve())

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_external_folder_nonexistent_path(self, async_client: AsyncClient, db_session):
        """Verify 400 for non-existent path."""
        data = {
            "name": "Bad Path",
            "external_path": "/nonexistent/path/that/does/not/exist",
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        assert response.status_code == 400
        assert "does not exist" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    @pytest.mark.skipif(
        __import__("os").name == "nt",
        reason="System directory test uses /proc which doesn't exist on Windows",
    )
    async def test_create_external_folder_system_dir_blocked(self, async_client: AsyncClient, db_session):
        """Verify system directories are blocked."""
        data = {
            "name": "System",
            "external_path": "/proc",
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        assert response.status_code == 400
        assert "system directory" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_external_folder_file_not_dir(self, async_client: AsyncClient, db_session, tmp_path):
        """Verify 400 when path is a file, not directory."""
        file_path = tmp_path / "not_a_dir.txt"
        file_path.write_text("hello")
        data = {
            "name": "Not A Dir",
            "external_path": str(file_path),
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        assert response.status_code == 400
        assert "not a directory" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_external_folder_duplicate_path(self, async_client: AsyncClient, db_session, external_dir):
        """Verify 409 when same path already linked."""
        data = {
            "name": "First",
            "external_path": str(external_dir),
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        assert response.status_code == 200

        data["name"] = "Duplicate"
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        assert response.status_code == 409
        assert "already exists" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_external_folder_appears_in_tree(self, async_client: AsyncClient, db_session, external_dir):
        """Verify external folder shows up in folder tree with external fields."""
        data = {
            "name": "My NAS",
            "external_path": str(external_dir),
            "readonly": True,
        }
        await async_client.post("/api/v1/library/folders/external", json=data)

        response = await async_client.get("/api/v1/library/folders")
        assert response.status_code == 200
        folders = response.json()
        ext_folder = next((f for f in folders if f["name"] == "My NAS"), None)
        assert ext_folder is not None
        assert ext_folder["is_external"] is True
        assert ext_folder["external_readonly"] is True


async def scan_and_wait(async_client: AsyncClient, folder_id: int) -> dict:
    """Start a scan, wait for the worker, and return the finished job row.

    ⚠️ The endpoint answers 202 the instant the job exists — the walk is a
    background task now, because doing it inside the request held SQLite's write
    lock for the whole of it. A test that reads the library straight after the
    POST is racing the worker, and on a small tmp_path it usually wins.
    """
    from backend.app.services import library_scan

    response = await async_client.post(f"/api/v1/library/folders/{folder_id}/scan")
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]

    task = library_scan._running.get(job_id)
    if task is not None:
        await task

    job = (await async_client.get(f"/api/v1/library/scan-jobs/{job_id}")).json()
    assert job["status"] == "finished", job
    return job


class TestExternalFolderScan:
    """Tests for POST /library/folders/{id}/scan."""

    @pytest.fixture
    def external_dir(self, tmp_path):
        """Create a temporary directory with test files."""
        ext_dir = tmp_path / "prints"
        ext_dir.mkdir()
        (ext_dir / "benchy.3mf").write_bytes(b"fake3mf")
        (ext_dir / "bracket.stl").write_bytes(b"fakestl")
        (ext_dir / "print.gcode").write_text("G28\nG1 X10 Y10")
        (ext_dir / "readme.txt").write_text("not a print file")
        (ext_dir / ".hidden.3mf").write_bytes(b"hidden")
        sub = ext_dir / "subfolder"
        sub.mkdir()
        (sub / "nested.stl").write_bytes(b"nested")
        return ext_dir

    @pytest.fixture
    async def external_folder(self, async_client, db_session, external_dir):
        """Create an external folder via API."""
        data = {
            "name": "Scan Test",
            "external_path": str(external_dir),
            "readonly": True,
            "show_hidden": False,
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        return response.json()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_discovers_files(self, async_client: AsyncClient, db_session, external_folder):
        """Verify scan discovers supported files."""
        job = await scan_and_wait(async_client, external_folder["id"])
        # Should find: benchy.3mf, bracket.stl, print.gcode, subfolder/nested.stl
        # Should skip: readme.txt (unsupported), .hidden.3mf (hidden)
        assert job["files_added"] == 4
        assert job["files_removed"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_skips_hidden_files(self, async_client: AsyncClient, db_session, external_folder):
        """Verify hidden files are skipped by default."""
        await scan_and_wait(async_client, external_folder["id"])

        # List files in folder
        response = await async_client.get(f"/api/v1/library/files?folder_id={external_folder['id']}")
        assert response.status_code == 200
        files = response.json()
        filenames = [f["filename"] for f in files]
        assert ".hidden.3mf" not in filenames

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_shows_hidden_when_enabled(self, async_client: AsyncClient, db_session, external_dir):
        """Verify hidden files found when show_hidden=True."""
        data = {
            "name": "Show Hidden Test",
            "external_path": str(external_dir),
            "show_hidden": True,
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        folder = response.json()

        job = await scan_and_wait(async_client, folder["id"])
        # Now should also find .hidden.3mf → 5 total
        assert job["files_added"] == 5

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_idempotent(self, async_client: AsyncClient, db_session, external_folder):
        """Verify scanning twice doesn't duplicate files."""
        first = await scan_and_wait(async_client, external_folder["id"])
        assert first["files_added"] == 4

        second = await scan_and_wait(async_client, external_folder["id"])
        assert second["files_added"] == 0
        assert second["files_removed"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_records_each_files_real_mtime(
        self, async_client: AsyncClient, db_session, external_folder, external_dir
    ):
        """#2680: the scan stamps the on-disk mtime, distinct per file.

        The bug this closes is not that the value was wrong but that there was
        none: every discovered row got the scan instant as its ``created_at``,
        so an external folder sorted by date was sorting on a tie and came out
        in whatever order the walk happened to produce.
        """
        import os

        # Distinct, well-separated mtimes so a tie is unmistakable in the result.
        os.utime(external_dir / "benchy.3mf", (1_600_000_000, 1_600_000_000))
        os.utime(external_dir / "bracket.stl", (1_700_000_000, 1_700_000_000))

        await scan_and_wait(async_client, external_folder["id"])

        files = (await async_client.get(f"/api/v1/library/files?folder_id={external_folder['id']}")).json()
        by_name = {f["filename"]: f for f in files}
        assert by_name["benchy.3mf"]["fs_modified_at"] is not None
        assert by_name["bracket.stl"]["fs_modified_at"] is not None
        assert by_name["benchy.3mf"]["fs_modified_at"] < by_name["bracket.stl"]["fs_modified_at"]
        # 1_600_000_000 is 2020-09-13 UTC — asserted so a local-time conversion,
        # which would still order correctly, cannot pass unnoticed.
        assert by_name["benchy.3mf"]["fs_modified_at"].startswith("2020-09-13")

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rescan_refreshes_the_mtime_of_a_file_it_already_knows(
        self, async_client: AsyncClient, db_session, external_folder, external_dir
    ):
        """#2680: a file edited on disk gets its new mtime on the next scan.

        The already-tracked branch used to ``continue`` before the ``stat`` call,
        so nothing about a known file was ever re-read. Recording the mtime only
        at discovery would have left every mount frozen at its import date —
        correct on the day it was added and progressively wrong afterwards.
        """
        import os

        os.utime(external_dir / "benchy.3mf", (1_600_000_000, 1_600_000_000))
        await scan_and_wait(async_client, external_folder["id"])

        os.utime(external_dir / "benchy.3mf", (1_800_000_000, 1_800_000_000))
        second = await scan_and_wait(async_client, external_folder["id"])
        assert second["files_added"] == 0  # nothing new — this is a refresh

        files = (await async_client.get(f"/api/v1/library/files?folder_id={external_folder['id']}")).json()
        benchy = next(f for f in files if f["filename"] == "benchy.3mf")
        assert benchy["fs_modified_at"].startswith("2027-01-15")  # 1_800_000_000 UTC

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_removes_deleted_files(
        self, async_client: AsyncClient, db_session, external_folder, external_dir
    ):
        """Verify scan removes entries for files no longer on disk."""
        await scan_and_wait(async_client, external_folder["id"])

        # Delete a file from disk
        (external_dir / "bracket.stl").unlink()

        job = await scan_and_wait(async_client, external_folder["id"])
        assert job["files_removed"] == 1
        assert job["files_added"] == 0
        # ⚠️ The walk still saw three files, so the deletion guard had no
        # reason to fire. It exists for the *empty* walk — see the unit tests.
        assert job["skipped_deletions"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_non_external_folder_fails(self, async_client: AsyncClient, db_session):
        """Verify scan fails on regular (non-external) folder."""
        # Create a regular folder
        data = {"name": "Regular Folder"}
        response = await async_client.post("/api/v1/library/folders", json=data)
        folder = response.json()

        response = await async_client.post(f"/api/v1/library/folders/{folder['id']}/scan")
        assert response.status_code == 400
        assert "not an external" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_scan_files_marked_external(self, async_client: AsyncClient, db_session, external_folder):
        """Verify scanned files have is_external=True."""
        await scan_and_wait(async_client, external_folder["id"])

        response = await async_client.get(f"/api/v1/library/files?folder_id={external_folder['id']}")
        files = response.json()
        assert len(files) > 0
        for f in files:
            assert f["is_external"] is True


class TestExternalFolderProtections:
    """Tests for read-only protections on external folders."""

    @pytest.fixture
    def external_dir(self, tmp_path):
        ext_dir = tmp_path / "readonly_share"
        ext_dir.mkdir()
        (ext_dir / "test.stl").write_bytes(b"fakestl")
        return ext_dir

    @pytest.fixture
    async def readonly_folder(self, async_client, db_session, external_dir):
        """Create a read-only external folder with files scanned."""
        data = {
            "name": "Read Only",
            "external_path": str(external_dir),
            "readonly": True,
        }
        response = await async_client.post("/api/v1/library/folders/external", json=data)
        folder = response.json()
        await scan_and_wait(async_client, folder["id"])
        return folder

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_upload_to_readonly_folder_blocked(self, async_client: AsyncClient, db_session, readonly_folder):
        """Verify uploads to read-only external folders are blocked."""
        import io

        file_content = io.BytesIO(b"test content")
        response = await async_client.post(
            f"/api/v1/library/files?folder_id={readonly_folder['id']}",
            files={"file": ("test.gcode", file_content, "application/octet-stream")},
        )
        assert response.status_code == 403
        assert "read-only" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_move_to_readonly_folder_blocked(self, async_client: AsyncClient, db_session, readonly_folder):
        """Verify moving files to read-only external folder is blocked."""
        from backend.app.models.library import LibraryFile

        # Create a regular file
        lib_file = LibraryFile(
            filename="regular.3mf",
            file_path="/test/regular.3mf",
            file_size=1024,
            file_type="3mf",
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)

        data = {"file_ids": [lib_file.id], "folder_id": readonly_folder["id"]}
        response = await async_client.post("/api/v1/library/files/move", json=data)
        assert response.status_code == 403
        assert "read-only" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_external_files_cannot_be_moved_out(self, async_client: AsyncClient, db_session, readonly_folder):
        """Verify external files can't be moved to other folders."""
        # Get the external file ID
        response = await async_client.get(f"/api/v1/library/files?folder_id={readonly_folder['id']}")
        files = response.json()
        assert len(files) > 0
        ext_file_id = files[0]["id"]

        # Try to move to root
        data = {"file_ids": [ext_file_id], "folder_id": None}
        response = await async_client.post("/api/v1/library/files/move", json=data)
        assert response.status_code == 200
        # File should be skipped, not moved
        result = response.json()
        assert result["moved"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_external_file_removes_db_only(
        self, async_client: AsyncClient, db_session, readonly_folder, external_dir
    ):
        """Verify deleting an external file only removes DB entry, not the file on disk."""
        response = await async_client.get(f"/api/v1/library/files?folder_id={readonly_folder['id']}")
        files = response.json()
        ext_file_id = files[0]["id"]
        ext_filename = files[0]["filename"]

        # Delete via API
        response = await async_client.delete(f"/api/v1/library/files/{ext_file_id}")
        assert response.status_code == 200

        # File should still exist on disk
        assert (external_dir / ext_filename).exists()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_external_folder_preserves_files(
        self, async_client: AsyncClient, db_session, readonly_folder, external_dir
    ):
        """Verify deleting an external folder doesn't delete files from disk."""
        response = await async_client.delete(f"/api/v1/library/folders/{readonly_folder['id']}")
        assert response.status_code == 200

        # Files should still exist on disk
        assert (external_dir / "test.stl").exists()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_zip_to_readonly_folder_blocked(self, async_client: AsyncClient, db_session, readonly_folder):
        """Verify ZIP extraction to read-only external folder is blocked."""
        import io
        import zipfile

        # Create a minimal zip
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("test.stl", b"fakestl")
        buf.seek(0)

        response = await async_client.post(
            f"/api/v1/library/files/extract-zip?folder_id={readonly_folder['id']}",
            files={"file": ("test.zip", buf, "application/zip")},
        )
        assert response.status_code == 403
        assert "read-only" in response.json()["detail"].lower()
