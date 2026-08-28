"""Integration tests for Library API endpoints."""

import io
import tempfile
import zipfile
from pathlib import Path

import pytest
from httpx import AsyncClient


class TestLibraryFoldersAPI:
    """Integration tests for library folders endpoints."""

    @pytest.fixture
    async def folder_factory(self, db_session):
        """Factory to create test folders."""
        _counter = [0]

        async def _create_folder(**kwargs):
            from backend.app.models.library import LibraryFolder

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "name": f"Test Folder {counter}",
            }
            defaults.update(kwargs)

            folder = LibraryFolder(**defaults)
            db_session.add(folder)
            await db_session.commit()
            await db_session.refresh(folder)
            return folder

        return _create_folder

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_folders_empty(self, async_client: AsyncClient, db_session):
        """Verify empty folder list returns empty array."""
        response = await async_client.get("/api/v1/library/folders")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_folder(self, async_client: AsyncClient, db_session):
        """Verify folder can be created."""
        data = {"name": "New Folder"}
        response = await async_client.post("/api/v1/library/folders", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "New Folder"
        assert result["id"] is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_create_nested_folder(self, async_client: AsyncClient, folder_factory, db_session):
        """Verify nested folder can be created."""
        parent = await folder_factory(name="Parent")
        data = {"name": "Child", "parent_id": parent.id}
        response = await async_client.post("/api/v1/library/folders", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "Child"
        assert result["parent_id"] == parent.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_in_a_writable_external_is_a_real_directory(
        self, async_client: AsyncClient, db_session, tmp_path
    ):
        """Inside a writable external the folder is a REAL directory on the
        share, registered in the scanner's own row shape — a virtual row
        there would be a phantom that looks like part of the NAS while its
        files live in managed storage (live, 2026-08-28)."""
        from backend.app.models.library import LibraryFolder

        ext = LibraryFolder(name="NAS", is_external=True, external_path=str(tmp_path), external_readonly=False)
        db_session.add(ext)
        await db_session.commit()
        await db_session.refresh(ext)

        response = await async_client.post("/api/v1/library/folders", json={"name": "parts", "parent_id": ext.id})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["is_external"] is True
        assert body["external_path"] == str(tmp_path / "parts")
        assert (tmp_path / "parts").is_dir()

        # Same name again: the directory exists — refused, not duplicated.
        repeat = await async_client.post("/api/v1/library/folders", json={"name": "parts", "parent_id": ext.id})
        assert repeat.status_code == 409

        # A separator-bearing name must not nest deeper than asked.
        sneaky = await async_client.post("/api/v1/library/folders", json={"name": "a/b", "parent_id": ext.id})
        assert sneaky.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_refused_in_a_readonly_external(self, async_client: AsyncClient, db_session, tmp_path):
        from backend.app.models.library import LibraryFolder

        ext = LibraryFolder(name="NAS-RO", is_external=True, external_path=str(tmp_path), external_readonly=True)
        db_session.add(ext)
        await db_session.commit()
        await db_session.refresh(ext)

        response = await async_client.post("/api/v1/library/folders", json={"name": "phantom", "parent_id": ext.id})
        assert response.status_code == 403

    async def test_get_folder(self, async_client: AsyncClient, folder_factory, db_session):
        """Verify single folder can be retrieved."""
        folder = await folder_factory(name="Test Folder")
        response = await async_client.get(f"/api/v1/library/folders/{folder.id}")
        assert response.status_code == 200
        result = response.json()
        assert result["id"] == folder.id
        assert result["name"] == "Test Folder"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_folder_not_found(self, async_client: AsyncClient, db_session):
        """Verify 404 for non-existent folder."""
        response = await async_client.get("/api/v1/library/folders/9999")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_folder(self, async_client: AsyncClient, folder_factory, db_session):
        """Verify folder can be updated."""
        folder = await folder_factory(name="Old Name")
        data = {"name": "New Name"}
        response = await async_client.put(f"/api/v1/library/folders/{folder.id}", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["name"] == "New Name"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_folder(self, async_client: AsyncClient, folder_factory, db_session):
        """Verify folder can be deleted."""
        folder = await folder_factory()
        response = await async_client.delete(f"/api/v1/library/folders/{folder.id}")
        assert response.status_code == 200
        result = response.json()
        assert result.get("message") or result.get("success", True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_tree_exposes_latest_activity_at_from_files(
        self, async_client: AsyncClient, folder_factory, db_session
    ):
        """#1770: folder list returns latest_activity_at = MAX(folder.updated_at,
        MAX(immediate-child file.updated_at)) so the frontend can sort by
        recent activity. Adding a file with a later updated_at must bubble it.
        """
        from datetime import datetime, timedelta

        from backend.app.models.library import LibraryFile

        folder = await folder_factory(name="Active Folder")
        # File whose updated_at is well after the folder's. Activity should
        # surface this timestamp, not the folder's stale one.
        future = datetime.utcnow() + timedelta(hours=24)
        db_session.add(
            LibraryFile(
                folder_id=folder.id,
                filename="model.3mf",
                file_path="library/model.3mf",
                file_type="3mf",
                file_size=123,
                updated_at=future,
            )
        )
        await db_session.commit()

        response = await async_client.get("/api/v1/library/folders")
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 1
        item = items[0]
        assert item["id"] == folder.id
        assert item["latest_activity_at"] is not None
        # latest_activity_at should be at least the future stamp we set.
        assert item["latest_activity_at"] >= future.isoformat()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_tree_latest_activity_at_falls_back_to_folder_updated_at(
        self, async_client: AsyncClient, folder_factory, db_session
    ):
        """#1770: a folder with no files reports its own updated_at, not null --
        otherwise the activity sort would dump every empty folder to one end."""
        await folder_factory(name="Empty Folder")
        response = await async_client.get("/api/v1/library/folders")
        assert response.status_code == 200
        items = response.json()
        assert len(items) == 1
        item = items[0]
        # latest_activity_at == folder.updated_at when there are no files
        assert item["latest_activity_at"] is not None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_activity_bubbles_from_a_grandchild_not_just_a_child(
        self, async_client: AsyncClient, folder_factory, db_session
    ):
        """#2680: activity folds in the whole subtree, not one level.

        #1770 aggregated immediate children only, so a parent whose files all
        live two levels down reported its own stale ``updated_at`` and sorted as
        untouched. That is the normal shape of an imported model and of an
        external mount, which is why it read as "the date sort is broken".
        """
        from datetime import datetime, timedelta

        from backend.app.models.library import LibraryFile

        root = await folder_factory(name="Root")
        mid = await folder_factory(name="Mid", parent_id=root.id)
        leaf = await folder_factory(name="Leaf", parent_id=mid.id)

        future = datetime.utcnow() + timedelta(hours=24)
        db_session.add(
            LibraryFile(
                folder_id=leaf.id,
                filename="deep.3mf",
                file_path="library/deep.3mf",
                file_type="3mf",
                file_size=1,
                updated_at=future,
            )
        )
        await db_session.commit()

        items = (await async_client.get("/api/v1/library/folders")).json()
        assert len(items) == 1  # only Root is top-level
        root_item = items[0]
        assert root_item["name"] == "Root"
        # The grandchild's file is two levels below Root and must still surface.
        assert root_item["latest_activity_at"] >= future.isoformat()
        # ...and every level in between reports it too.
        mid_item = root_item["children"][0]
        assert mid_item["latest_activity_at"] >= future.isoformat()
        assert mid_item["children"][0]["latest_activity_at"] >= future.isoformat()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_folder_activity_prefers_the_on_disk_mtime(
        self, async_client: AsyncClient, folder_factory, db_session
    ):
        """#2680: ``fs_modified_at`` outranks ``updated_at`` on both row types.

        An external scan stamps one identical ``updated_at`` across every row it
        touches, so that column cannot order a scanned tree at all. m129 records
        the real mtime; COALESCE has to prefer it even when it is OLDER, which is
        the case this asserts — an ``updated_at`` of "now" would otherwise mask
        a file that has genuinely not been touched since last year.
        """
        from datetime import datetime, timedelta

        from backend.app.models.library import LibraryFile

        past = datetime.utcnow() - timedelta(days=365)
        folder = await folder_factory(name="Mount", fs_modified_at=past)
        db_session.add(
            LibraryFile(
                folder_id=folder.id,
                filename="old.3mf",
                file_path="/mnt/nas/old.3mf",
                file_type="3mf",
                file_size=1,
                is_external=True,
                fs_modified_at=past,
            )
        )
        await db_session.commit()

        items = (await async_client.get("/api/v1/library/folders")).json()
        assert items[0]["latest_activity_at"][:10] == past.date().isoformat()


class TestLibraryFilesAPI:
    """Integration tests for library files endpoints."""

    @pytest.fixture
    async def folder_factory(self, db_session):
        """Factory to create test folders."""
        _counter = [0]

        async def _create_folder(**kwargs):
            from backend.app.models.library import LibraryFolder

            _counter[0] += 1
            counter = _counter[0]

            defaults = {"name": f"Test Folder {counter}"}
            defaults.update(kwargs)

            folder = LibraryFolder(**defaults)
            db_session.add(folder)
            await db_session.commit()
            await db_session.refresh(folder)
            return folder

        return _create_folder

    @pytest.fixture
    async def file_factory(self, db_session):
        """Factory to create test files."""
        _counter = [0]

        async def _create_file(**kwargs):
            from backend.app.models.library import LibraryFile

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "filename": f"test_file_{counter}.3mf",
                "file_path": f"/test/path/test_file_{counter}.3mf",
                "file_size": 1024,
                "file_type": "3mf",
            }
            defaults.update(kwargs)

            lib_file = LibraryFile(**defaults)
            db_session.add(lib_file)
            await db_session.commit()
            await db_session.refresh(lib_file)
            return lib_file

        return _create_file

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_empty(self, async_client: AsyncClient, db_session):
        """Verify empty file list returns empty array."""
        response = await async_client.get("/api/v1/library/files")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_in_folder(self, async_client: AsyncClient, folder_factory, file_factory, db_session):
        """Verify files can be filtered by folder."""
        folder = await folder_factory()
        file1 = await file_factory(folder_id=folder.id)
        await file_factory()  # File in root (no folder)

        response = await async_client.get(f"/api/v1/library/files?folder_id={folder.id}")
        assert response.status_code == 200
        result = response.json()
        assert len(result) == 1
        assert result[0]["id"] == file1.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_internal_only(self, async_client: AsyncClient, folder_factory, file_factory, db_session):
        """#1621: `internal_only=true` restricts the listing to files in managed
        storage (`is_external=False`) so a linked NAS with hundreds of files
        doesn't drown the user's own uploads in the "All Files" sidebar view."""
        internal_folder = await folder_factory(name="My uploads")
        external_folder = await folder_factory(name="NAS", is_external=True, external_path="/mnt/nas")

        internal_file = await file_factory(folder_id=internal_folder.id, filename="mine.3mf", is_external=False)
        await file_factory(folder_id=external_folder.id, filename="nas.3mf", is_external=True)
        root_file = await file_factory(filename="root.3mf", is_external=False)  # Root-uploaded is always internal.

        response = await async_client.get("/api/v1/library/files?include_root=false&internal_only=true")
        assert response.status_code == 200
        ids = {f["id"] for f in response.json()}
        assert ids == {internal_file.id, root_file.id}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_external_only(self, async_client: AsyncClient, folder_factory, file_factory, db_session):
        """#1621 symmetric: `external_only=true` returns the combined view
        across every linked external folder so users with several mounts can
        see all external content in one place without clicking each folder."""
        internal_folder = await folder_factory(name="My uploads")
        nas_a = await folder_factory(name="NAS A", is_external=True, external_path="/mnt/a")
        nas_b = await folder_factory(name="NAS B", is_external=True, external_path="/mnt/b")

        await file_factory(folder_id=internal_folder.id, filename="mine.3mf", is_external=False)
        ext_a = await file_factory(folder_id=nas_a.id, filename="a.3mf", is_external=True)
        ext_b = await file_factory(folder_id=nas_b.id, filename="b.3mf", is_external=True)

        response = await async_client.get("/api/v1/library/files?include_root=false&external_only=true")
        assert response.status_code == 200
        ids = {f["id"] for f in response.json()}
        assert ids == {ext_a.id, ext_b.id}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_internal_and_external_mutually_exclusive(self, async_client: AsyncClient, db_session):
        """Both flags together is a caller bug — fail loud (400) rather than
        silently picking one, so a frontend regression is caught immediately."""
        response = await async_client.get("/api/v1/library/files?internal_only=true&external_only=true")
        assert response.status_code == 400
        assert "mutually exclusive" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_file(self, async_client: AsyncClient, file_factory, db_session):
        """Verify single file can be retrieved."""
        lib_file = await file_factory(filename="test.3mf")
        response = await async_client.get(f"/api/v1/library/files/{lib_file.id}")
        assert response.status_code == 200
        result = response.json()
        assert result["id"] == lib_file.id
        assert result["filename"] == "test.3mf"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_file_not_found(self, async_client: AsyncClient, db_session):
        """Verify 404 for non-existent file."""
        response = await async_client.get("/api/v1/library/files/9999")
        assert response.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_file(self, async_client: AsyncClient, file_factory, db_session):
        """Verify file can be deleted."""
        lib_file = await file_factory()
        response = await async_client.delete(f"/api/v1/library/files/{lib_file.id}")
        assert response.status_code == 200
        result = response.json()
        assert result.get("message") or result.get("success", True)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rename_file(self, async_client: AsyncClient, file_factory, db_session):
        """Verify file can be renamed."""
        lib_file = await file_factory(filename="old_name.3mf")
        data = {"filename": "new_name.3mf"}
        response = await async_client.put(f"/api/v1/library/files/{lib_file.id}", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["filename"] == "new_name.3mf"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rename_file_invalid_path_separator(self, async_client: AsyncClient, file_factory, db_session):
        """Verify file rename rejects the forward-slash path separator.

        Since #1540 the rename validates the full FAT32-illegal set (not just
        separators), so the error names the offending character.
        """
        lib_file = await file_factory(filename="test.3mf")
        data = {"filename": "path/to/file.3mf"}
        response = await async_client.put(f"/api/v1/library/files/{lib_file.id}", json=data)
        assert response.status_code == 400
        assert "invalid character: /" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_rename_file_invalid_backslash(self, async_client: AsyncClient, file_factory, db_session):
        """Verify file rename rejects the backslash path separator."""
        lib_file = await file_factory(filename="test.3mf")
        data = {"filename": "path\\to\\file.3mf"}
        response = await async_client.put(f"/api/v1/library/files/{lib_file.id}", json=data)
        assert response.status_code == 400
        assert "invalid character: \\" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_library_stats(self, async_client: AsyncClient, folder_factory, file_factory, db_session):
        """Verify library stats endpoint returns counts."""
        await folder_factory()
        await folder_factory()
        await file_factory()

        response = await async_client.get("/api/v1/library/stats")
        assert response.status_code == 200
        result = response.json()
        assert result["total_folders"] == 2
        assert result["total_files"] == 1

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_file_list_includes_user_tracking_fields(self, async_client: AsyncClient, file_factory, db_session):
        """Verify file list response includes user tracking fields (Issue #206)."""
        lib_file = await file_factory(filename="test.3mf")
        response = await async_client.get("/api/v1/library/files?include_root=false")
        assert response.status_code == 200
        result = response.json()
        assert len(result) >= 1
        # Find our test file
        test_file = next((f for f in result if f["id"] == lib_file.id), None)
        assert test_file is not None
        # User tracking fields should be present (even if null)
        assert "created_by_id" in test_file
        assert "created_by_username" in test_file

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_file_detail_includes_user_tracking_fields(self, async_client: AsyncClient, file_factory, db_session):
        """Verify file detail response includes user tracking fields (Issue #206)."""
        lib_file = await file_factory(filename="test_detail.3mf")
        response = await async_client.get(f"/api/v1/library/files/{lib_file.id}")
        assert response.status_code == 200
        result = response.json()
        # User tracking fields should be present (even if null)
        assert "created_by_id" in result
        assert "created_by_username" in result

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_file_with_user_tracking(self, async_client: AsyncClient, db_session):
        """Verify file created with user shows username in response (Issue #206)."""
        from backend.app.models.library import LibraryFile
        from backend.app.models.user import User

        # Create a test user
        user = User(username="testuploader", password_hash="fakehash", role="user")
        db_session.add(user)
        await db_session.flush()

        # Create a file with created_by_id set
        lib_file = LibraryFile(
            filename="user_uploaded.3mf",
            file_path="/test/user_uploaded.3mf",
            file_size=2048,
            file_type="3mf",
            created_by_id=user.id,
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)

        # Verify file detail shows username
        response = await async_client.get(f"/api/v1/library/files/{lib_file.id}")
        assert response.status_code == 200
        result = response.json()
        assert result["created_by_id"] == user.id
        assert result["created_by_username"] == "testuploader"

        # Verify file list also shows username
        response = await async_client.get("/api/v1/library/files?include_root=false")
        assert response.status_code == 200
        files = response.json()
        test_file = next((f for f in files if f["id"] == lib_file.id), None)
        assert test_file is not None
        assert test_file["created_by_id"] == user.id
        assert test_file["created_by_username"] == "testuploader"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_recursive_includes_subfolders(
        self, async_client: AsyncClient, folder_factory, file_factory, db_session
    ):
        """#1268: ?recursive=true with folder_id must include every descendant.

        Tree:
            toys/             <- direct file "robot_top.3mf"
              cars/           <- child of toys, "robot_car.3mf"
                race/         <- grandchild, "robot_race.3mf"
            other/            <- unrelated, "robot_other.3mf" (must NOT appear)
        """
        toys = await folder_factory(name="toys")
        cars = await folder_factory(name="cars", parent_id=toys.id)
        race = await folder_factory(name="race", parent_id=cars.id)
        other = await folder_factory(name="other")

        top = await file_factory(folder_id=toys.id, filename="robot_top.3mf")
        mid = await file_factory(folder_id=cars.id, filename="robot_car.3mf")
        deep = await file_factory(folder_id=race.id, filename="robot_race.3mf")
        await file_factory(folder_id=other.id, filename="robot_other.3mf")

        # Non-recursive: only the file directly under toys.
        r = await async_client.get(f"/api/v1/library/files?folder_id={toys.id}")
        assert r.status_code == 200
        assert {f["id"] for f in r.json()} == {top.id}

        # Recursive: toys + cars + race files, but NOT other/.
        r = await async_client.get(f"/api/v1/library/files?folder_id={toys.id}&recursive=true")
        assert r.status_code == 200
        assert {f["id"] for f in r.json()} == {top.id, mid.id, deep.id}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_recursive_without_folder_id_is_noop(
        self, async_client: AsyncClient, folder_factory, file_factory, db_session
    ):
        """recursive=true is meaningful only with folder_id -- without it the
        existing include_root branch handles scoping. Just confirming the new
        param doesn't shadow that path."""
        folder = await folder_factory()
        f_in = await file_factory(folder_id=folder.id)
        f_root = await file_factory()

        r = await async_client.get("/api/v1/library/files?include_root=false&recursive=true")
        assert r.status_code == 200
        assert {f["id"] for f in r.json()} == {f_in.id, f_root.id}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_folder_readme_returns_first_markdown(
        self, async_client: AsyncClient, folder_factory, file_factory, db_session
    ):
        """#1268: /folders/{id}/readme reads on-disk content of the first .md."""
        folder = await folder_factory()
        # newline="" so Python doesn't translate \n -> \r\n on Windows; the
        # endpoint returns the exact on-disk bytes and the assert is exact.
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8", newline="") as f:
            f.write("# Robot\n\nA cute little robot.")
            md_path = f.name
        try:
            await file_factory(
                folder_id=folder.id,
                filename="README.md",
                file_path=md_path,
                file_type="md",
                file_size=Path(md_path).stat().st_size,
            )
            r = await async_client.get(f"/api/v1/library/folders/{folder.id}/readme")
            assert r.status_code == 200
            body = r.json()
            assert body["filename"] == "README.md"
            assert body["content"] == "# Robot\n\nA cute little robot."
            assert body["truncated"] is False
        finally:
            import os

            os.unlink(md_path)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_folder_readme_prefers_readme_over_other_md(
        self, async_client: AsyncClient, folder_factory, file_factory, db_session
    ):
        """When the folder has multiple .md files, README.md / description.md
        wins regardless of insertion order or filename case."""
        folder = await folder_factory()
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8", newline="") as f:
            f.write("notes notes notes")
            notes_path = f.name
        with tempfile.NamedTemporaryFile(suffix=".md", delete=False, mode="w", encoding="utf-8", newline="") as f:
            f.write("the real one")
            readme_path = f.name
        try:
            # notes.md inserted FIRST -- naive ordering would pick this one.
            await file_factory(
                folder_id=folder.id,
                filename="notes.md",
                file_path=notes_path,
                file_type="md",
            )
            await file_factory(
                folder_id=folder.id,
                filename="readme.md",  # lowercase to confirm case-insensitive match
                file_path=readme_path,
                file_type="md",
            )
            r = await async_client.get(f"/api/v1/library/folders/{folder.id}/readme")
            assert r.status_code == 200
            assert r.json()["filename"] == "readme.md"
            assert r.json()["content"] == "the real one"
        finally:
            import os

            os.unlink(notes_path)
            os.unlink(readme_path)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_folder_readme_404_when_no_markdown(
        self, async_client: AsyncClient, folder_factory, file_factory, db_session
    ):
        """No .md in the folder -> 404 so the FE can hide the side panel."""
        folder = await folder_factory()
        await file_factory(folder_id=folder.id, filename="model.3mf", file_type="3mf")
        r = await async_client.get(f"/api/v1/library/folders/{folder.id}/readme")
        assert r.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_folder_readme_404_when_folder_missing(self, async_client: AsyncClient, db_session):
        r = await async_client.get("/api/v1/library/folders/999999/readme")
        assert r.status_code == 404


async def _printer_queue_for(db_session, printer):
    """The printer's own queue. ``PrinterQueue.id == printer_id`` — the
    invariant the scheduler relies on."""
    from backend.app.models.printer_queue import PrinterQueue

    queue = PrinterQueue(printer_id=printer.id)
    db_session.add(queue)
    await db_session.commit()
    await db_session.refresh(queue)
    return queue


class TestLibraryAddToQueueAPI:
    """Queueing a library file is refused when it cannot be printed.

    Written against ``/library/files/add-to-queue``, which could not create a
    queue entry at all; moved to its bulk replacement; and moved again when the
    bulk route was replaced by opening the Schedule dialog once per file. What
    they check has survived all three: a missing file and a non-sliced file are
    each REFUSED, not accepted and left to fail at dispatch.
    """

    @pytest.fixture
    async def printer_factory(self, db_session):
        """Factory to create test printers."""
        _counter = [0]

        async def _create_printer(**kwargs):
            from backend.app.models.printer import Printer

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "name": f"Test Printer {counter}",
                "ip_address": f"192.168.1.{100 + counter}",
                "serial_number": f"TESTSERIAL{counter:04d}",
                "access_code": "12345678",
                "model": "X1C",
            }
            defaults.update(kwargs)

            printer = Printer(**defaults)
            db_session.add(printer)
            await db_session.commit()
            await db_session.refresh(printer)
            return printer

        return _create_printer

    @pytest.fixture
    async def library_file_factory(self, db_session):
        """Factory to create test library files."""
        _counter = [0]

        async def _create_library_file(**kwargs):
            from backend.app.models.library import LibraryFile

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "filename": f"test_file_{counter}.gcode.3mf",
                "file_path": f"/test/path/test_file_{counter}.gcode.3mf",
                "file_size": 1024,
                "file_type": "3mf",
            }
            defaults.update(kwargs)

            lib_file = LibraryFile(**defaults)
            db_session.add(lib_file)
            await db_session.commit()
            await db_session.refresh(lib_file)
            return lib_file

        return _create_library_file

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_to_queue_file_not_found(self, async_client: AsyncClient, printer_factory, db_session):
        """Verify error for non-existent file."""
        printer = await printer_factory()
        queue = await _printer_queue_for(db_session, printer)
        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "library_file_id": 9999},
        )
        assert response.status_code == 400
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_add_non_sliced_file_to_queue_fails(
        self, async_client: AsyncClient, printer_factory, library_file_factory, db_session
    ):
        """Verify non-sliced file cannot be added to queue."""
        lib_file = await library_file_factory(
            filename="model.stl",
            file_path="/test/path/model.stl",
            file_type="stl",
        )

        printer = await printer_factory()
        queue = await _printer_queue_for(db_session, printer)
        response = await async_client.post(
            "/api/v1/queue/",
            json={"queue_id": queue.id, "library_file_id": lib_file.id},
        )
        assert response.status_code == 400
        assert "sliced" in response.json()["detail"].lower()


class TestLibraryZipExtractAPI:
    """Integration tests for ZIP extraction endpoint."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extract_zip_invalid_file_type(self, async_client: AsyncClient, db_session):
        """Verify non-ZIP files are rejected."""
        # Create a fake file that's not a ZIP
        files = {"file": ("test.txt", b"This is not a zip file", "text/plain")}
        response = await async_client.post("/api/v1/library/files/extract-zip", files=files)
        assert response.status_code == 400
        assert "ZIP" in response.json()["detail"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extract_zip_basic(self, async_client: AsyncClient, db_session):
        """Verify basic ZIP extraction works."""
        import io

        # Create a simple ZIP file in memory
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("test1.txt", "Content of file 1")
            zf.writestr("test2.txt", "Content of file 2")
        zip_buffer.seek(0)

        files = {"file": ("test.zip", zip_buffer.read(), "application/zip")}
        response = await async_client.post("/api/v1/library/files/extract-zip", files=files)
        assert response.status_code == 200
        result = response.json()
        assert result["extracted"] == 2
        assert len(result["files"]) == 2
        assert len(result["errors"]) == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extract_zip_with_folders(self, async_client: AsyncClient, db_session):
        """Verify ZIP extraction preserves folder structure."""
        import io

        # Create a ZIP file with folder structure
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("folder1/file1.txt", "Content 1")
            zf.writestr("folder1/subfolder/file2.txt", "Content 2")
            zf.writestr("folder2/file3.txt", "Content 3")
        zip_buffer.seek(0)

        files = {"file": ("test.zip", zip_buffer.read(), "application/zip")}
        params = {"preserve_structure": "true"}
        response = await async_client.post("/api/v1/library/files/extract-zip", files=files, params=params)
        assert response.status_code == 200
        result = response.json()
        assert result["extracted"] == 3
        assert result["folders_created"] >= 3  # folder1, folder1/subfolder, folder2

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extract_zip_flat(self, async_client: AsyncClient, db_session):
        """Verify ZIP extraction can extract flat (no folders)."""
        import io

        # Create a ZIP file with folder structure
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("folder/file1.txt", "Content 1")
            zf.writestr("folder/file2.txt", "Content 2")
        zip_buffer.seek(0)

        files = {"file": ("test.zip", zip_buffer.read(), "application/zip")}
        params = {"preserve_structure": "false"}
        response = await async_client.post("/api/v1/library/files/extract-zip", files=files, params=params)
        assert response.status_code == 200
        result = response.json()
        assert result["extracted"] == 2
        assert result["folders_created"] == 0  # No folders created when flat

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extract_zip_skips_macos_files(self, async_client: AsyncClient, db_session):
        """Verify ZIP extraction skips __MACOSX and hidden files."""
        import io

        # Create a ZIP file with macOS junk files
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("real_file.txt", "Real content")
            zf.writestr("__MACOSX/._real_file.txt", "macOS metadata")
            zf.writestr(".hidden_file", "Hidden content")
        zip_buffer.seek(0)

        files = {"file": ("test.zip", zip_buffer.read(), "application/zip")}
        response = await async_client.post("/api/v1/library/files/extract-zip", files=files)
        assert response.status_code == 200
        result = response.json()
        assert result["extracted"] == 1  # Only real_file.txt
        assert result["files"][0]["filename"] == "real_file.txt"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extract_zip_create_folder_from_zip(self, async_client: AsyncClient, db_session):
        """Verify ZIP extraction creates a folder from the ZIP filename."""
        import io

        # Create a ZIP file with some files
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("file1.txt", "Content 1")
            zf.writestr("file2.txt", "Content 2")
        zip_buffer.seek(0)

        files = {"file": ("MyProject.zip", zip_buffer.read(), "application/zip")}
        params = {"create_folder_from_zip": "true", "preserve_structure": "false"}
        response = await async_client.post("/api/v1/library/files/extract-zip", files=files, params=params)
        assert response.status_code == 200
        result = response.json()
        assert result["extracted"] == 2
        assert result["folders_created"] == 1  # MyProject folder created

        # Verify the files are in a folder
        assert result["files"][0]["folder_id"] is not None
        assert result["files"][1]["folder_id"] is not None
        # Both files should be in the same folder
        assert result["files"][0]["folder_id"] == result["files"][1]["folder_id"]

        # Verify the folder was created with the right name
        folder_response = await async_client.get(f"/api/v1/library/folders/{result['files'][0]['folder_id']}")
        assert folder_response.status_code == 200
        folder = folder_response.json()
        assert folder["name"] == "MyProject"


class TestLibraryStlThumbnailAPI:
    """Integration tests for STL thumbnail generation endpoints."""

    @pytest.fixture
    async def file_factory(self, db_session):
        """Factory to create test files."""
        _counter = [0]

        async def _create_file(**kwargs):
            from backend.app.models.library import LibraryFile

            _counter[0] += 1
            counter = _counter[0]

            defaults = {
                "filename": f"test_model_{counter}.stl",
                "file_path": f"/test/path/test_model_{counter}.stl",
                "file_size": 1024,
                "file_type": "stl",
            }
            defaults.update(kwargs)

            lib_file = LibraryFile(**defaults)
            db_session.add(lib_file)
            await db_session.commit()
            await db_session.refresh(lib_file)
            return lib_file

        return _create_file

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_generate_thumbnails_empty(self, async_client: AsyncClient, db_session):
        """Verify batch thumbnail generation with no files."""
        data = {"all_missing": True}
        response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["processed"] == 0
        assert result["succeeded"] == 0
        assert result["failed"] == 0
        assert result["results"] == []

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_generate_thumbnails_no_criteria(self, async_client: AsyncClient, db_session):
        """Verify batch thumbnail generation with no criteria returns empty."""
        data = {}
        response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["processed"] == 0

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_generate_thumbnails_file_not_on_disk(
        self, async_client: AsyncClient, file_factory, db_session
    ):
        """Verify batch thumbnail generation handles missing files gracefully."""
        # Create a file in DB but not on disk
        stl_file = await file_factory(
            filename="missing.stl",
            file_path="/nonexistent/path/missing.stl",
            thumbnail_path=None,
        )

        data = {"file_ids": [stl_file.id]}
        response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json=data)
        assert response.status_code == 200
        result = response.json()
        assert result["processed"] == 1
        assert result["succeeded"] == 0
        assert result["failed"] == 1
        assert result["results"][0]["success"] is False
        assert "not found" in result["results"][0]["error"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_generate_thumbnails_with_real_stl(self, async_client: AsyncClient, db_session):
        """Verify batch thumbnail generation with a real STL file."""
        from backend.app.models.library import LibraryFile

        # Create a simple ASCII STL cube
        stl_content = """solid cube
facet normal 0 0 -1
  outer loop
    vertex 0 0 0
    vertex 1 0 0
    vertex 1 1 0
  endloop
endfacet
facet normal 0 0 1
  outer loop
    vertex 0 0 1
    vertex 1 1 1
    vertex 1 0 1
  endloop
endfacet
endsolid cube"""

        with tempfile.NamedTemporaryFile(suffix=".stl", delete=False, mode="w") as f:
            f.write(stl_content)
            stl_path = f.name

        try:
            # Create file in DB pointing to real STL
            lib_file = LibraryFile(
                filename="test_cube.stl",
                file_path=stl_path,
                file_size=len(stl_content),
                file_type="stl",
                thumbnail_path=None,
            )
            db_session.add(lib_file)
            await db_session.commit()
            await db_session.refresh(lib_file)

            data = {"file_ids": [lib_file.id]}
            response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json=data)
            assert response.status_code == 200
            result = response.json()
            assert result["processed"] == 1
            # Result depends on whether trimesh/matplotlib are installed
            # Either succeeds or fails gracefully
            assert result["succeeded"] + result["failed"] == 1
        finally:
            import os

            if os.path.exists(stl_path):
                os.unlink(stl_path)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_upload_sliced_gcode_3mf_collapses_to_gcode_type(self, async_client: AsyncClient, db_session):
        """Sliced ``.gcode.3mf`` upload must end up with file_type='gcode'
        and file_tags including BOTH 'gcode' and '3mf'.

        Pre-m035 the upload route stored ``file_type='3mf'`` for sliced
        outputs (because the trailing extension is ``.3mf``), inconsistent
        with the slicer-output route which stored ``"gcode"``. Helper +
        m035 backfill close this gap. Test guards the regression.
        """
        # A minimal but real ZIP container: the upload validator (#1401)
        # rejects non-ZIP .3mf bodies at upload time, so the body must carry
        # the ZIP magic. ThreeMFParser still won't find the expected 3MF
        # structure — that failure is non-fatal (logged) and shouldn't affect
        # file_type, which collapses to 'gcode' from the .gcode.3mf filename.
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Metadata/plate_1.gcode", b"; sliced gcode\n")
        files = {"file": ("output.gcode.3mf", buf.getvalue(), "application/octet-stream")}
        response = await async_client.post("/api/v1/library/files", files=files)
        assert response.status_code == 200
        result = response.json()
        assert result["filename"] == "output.gcode.3mf"
        assert result["file_type"] == "gcode", "Sliced 3MF must collapse to file_type='gcode'"
        # Composite tags carry both formats so the UI can render the
        # green 3MF + blue GCODE badge pair the user expects.
        tags = result.get("file_tags") or []
        assert "gcode" in tags
        assert "3mf" in tags

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_upload_file_with_stl_thumbnail_param(self, async_client: AsyncClient, db_session):
        """Verify file upload accepts generate_stl_thumbnails parameter."""
        # Create a simple STL file
        stl_content = b"solid test\nendsolid test"

        files = {"file": ("test.stl", stl_content, "application/octet-stream")}
        params = {"generate_stl_thumbnails": "false"}
        response = await async_client.post("/api/v1/library/files", files=files, params=params)
        assert response.status_code == 200
        result = response.json()
        assert result["filename"] == "test.stl"
        assert result["file_type"] == "stl"
        # No thumbnail should be generated when disabled
        assert result["thumbnail_path"] is None

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_extract_zip_with_stl_thumbnail_param(self, async_client: AsyncClient, db_session):
        """Verify ZIP extraction accepts generate_stl_thumbnails parameter."""
        # Create a ZIP file containing an STL
        stl_content = b"solid test\nendsolid test"
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("model.stl", stl_content)
        zip_buffer.seek(0)

        files = {"file": ("test.zip", zip_buffer.read(), "application/zip")}
        params = {"generate_stl_thumbnails": "false"}
        response = await async_client.post("/api/v1/library/files/extract-zip", files=files, params=params)
        assert response.status_code == 200
        result = response.json()
        assert result["extracted"] == 1
        assert result["files"][0]["filename"] == "model.stl"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_generate_thumbnails_by_folder(self, async_client: AsyncClient, file_factory, db_session):
        """Verify batch thumbnail generation can filter by folder."""
        from backend.app.models.library import LibraryFolder

        # Create a folder
        folder = LibraryFolder(name="STL Folder")
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)

        # Create STL file in folder (no thumbnail)
        stl_in_folder = await file_factory(
            filename="in_folder.stl",
            folder_id=folder.id,
            thumbnail_path=None,
        )

        # Create STL file at root (no thumbnail)
        _stl_at_root = await file_factory(
            filename="at_root.stl",
            folder_id=None,
            thumbnail_path=None,
        )

        # Request thumbnails only for files in folder
        data = {"folder_id": folder.id, "all_missing": True}
        response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json=data)
        assert response.status_code == 200
        result = response.json()
        # Should only process the file in the folder
        assert result["processed"] == 1
        assert result["results"][0]["file_id"] == stl_in_folder.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_batch_generate_thumbnails_all_missing(self, async_client: AsyncClient, file_factory, db_session):
        """Verify batch thumbnail generation finds all STL files missing thumbnails."""
        # Create files with and without thumbnails
        _stl_with_thumb = await file_factory(
            filename="with_thumb.stl",
            thumbnail_path="/some/path/thumb.png",
        )
        stl_without_thumb1 = await file_factory(
            filename="without_thumb1.stl",
            thumbnail_path=None,
        )
        stl_without_thumb2 = await file_factory(
            filename="without_thumb2.stl",
            thumbnail_path=None,
        )

        data = {"all_missing": True}
        response = await async_client.post("/api/v1/library/generate-stl-thumbnails", json=data)
        assert response.status_code == 200
        result = response.json()
        # Should only process files without thumbnails
        assert result["processed"] == 2
        file_ids = {r["file_id"] for r in result["results"]}
        assert stl_without_thumb1.id in file_ids
        assert stl_without_thumb2.id in file_ids


class TestLibraryPathHelpers:
    """Tests for path handling utilities used for backup portability."""

    def test_to_relative_path_converts_absolute(self):
        """Verify absolute paths are converted to relative paths."""
        from backend.app.api.routes.library import to_relative_path
        from backend.app.core.config import settings

        base_dir = str(settings.base_dir)
        abs_path = f"{base_dir}/archive/library/files/test.3mf"
        rel_path = to_relative_path(abs_path)

        assert not rel_path.startswith("/")
        assert rel_path.replace("\\", "/") == "archive/library/files/test.3mf"

    def test_to_relative_path_handles_path_object(self):
        """Verify Path objects are handled correctly."""
        from pathlib import Path

        from backend.app.api.routes.library import to_relative_path
        from backend.app.core.config import settings

        abs_path = Path(settings.base_dir) / "archive" / "test.3mf"
        rel_path = to_relative_path(abs_path)

        assert not rel_path.startswith("/")
        assert rel_path.replace("\\", "/") == "archive/test.3mf"

    def test_to_relative_path_returns_empty_for_empty_input(self):
        """Verify empty input returns empty string."""
        from backend.app.api.routes.library import to_relative_path

        assert to_relative_path("") == ""
        assert to_relative_path(None) == ""

    def test_to_absolute_path_converts_relative(self):
        """Verify relative paths are converted to absolute paths."""
        from backend.app.api.routes.library import to_absolute_path
        from backend.app.core.config import settings

        rel_path = "archive/library/files/test.3mf"
        abs_path = to_absolute_path(rel_path)

        assert abs_path is not None
        assert abs_path.is_absolute()
        expected = str(settings.base_dir / "archive" / "library" / "files" / "test.3mf")
        assert str(abs_path) == expected

    def test_to_absolute_path_handles_already_absolute(self):
        """Verify already absolute paths are returned as-is (for backwards compatibility)."""
        import os

        from backend.app.api.routes.library import to_absolute_path

        if os.name == "nt":
            abs_path_str = "C:\\data\\archive\\test.3mf"
        else:
            abs_path_str = "/data/archive/test.3mf"
        result = to_absolute_path(abs_path_str)

        assert result is not None
        assert str(result) == abs_path_str

    def test_to_absolute_path_returns_none_for_empty(self):
        """Verify None/empty input returns None."""
        from backend.app.api.routes.library import to_absolute_path

        assert to_absolute_path(None) is None
        assert to_absolute_path("") is None


class TestLibraryPermissions:
    """Tests for library permission enforcement."""

    @pytest.fixture
    async def auth_setup(self, db_session):
        """Set up auth with users of different permission levels."""
        from backend.app.core.auth import create_access_token, get_password_hash
        from backend.app.models.group import Group
        from backend.app.models.settings import Settings
        from backend.app.models.user import User

        # Enable auth
        settings = Settings(key="auth_enabled", value="true")
        db_session.add(settings)
        await db_session.commit()

        # Groups are auto-seeded during db init, but we need to commit them
        await db_session.commit()

        # Get groups
        from sqlalchemy import select

        admin_group = (await db_session.execute(select(Group).where(Group.name == "Administrators"))).scalar_one()
        operator_group = (await db_session.execute(select(Group).where(Group.name == "Operators"))).scalar_one()
        viewer_group = (await db_session.execute(select(Group).where(Group.name == "Viewers"))).scalar_one()

        password_hash = get_password_hash("password")

        # Create users
        admin_user = User(username="admin_lib", password_hash=password_hash, role="admin", is_active=True)
        admin_user.groups.append(admin_group)

        operator_user = User(username="operator_lib", password_hash=password_hash, is_active=True)
        operator_user.groups.append(operator_group)

        viewer_user = User(username="viewer_lib", password_hash=password_hash, is_active=True)
        viewer_user.groups.append(viewer_group)

        db_session.add_all([admin_user, operator_user, viewer_user])
        await db_session.commit()

        # Create tokens
        admin_token = create_access_token(data={"sub": admin_user.username})
        operator_token = create_access_token(data={"sub": operator_user.username})
        viewer_token = create_access_token(data={"sub": viewer_user.username})

        return {
            "admin_user": admin_user,
            "operator_user": operator_user,
            "viewer_user": viewer_user,
            "admin_token": admin_token,
            "operator_token": operator_token,
            "viewer_token": viewer_token,
        }

    @pytest.fixture
    async def test_file(self, db_session, auth_setup):
        """Create a test file owned by the operator user."""
        from backend.app.models.library import LibraryFile

        operator_user = auth_setup["operator_user"]
        lib_file = LibraryFile(
            filename="test.txt",
            file_path="archive/library/files/test.txt",
            file_type="txt",
            file_size=100,
            created_by_id=operator_user.id,
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)
        return lib_file

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_requires_library_read(self, async_client: AsyncClient, db_session, auth_setup):
        """Verify list_files requires library:read permission."""
        viewer_token = auth_setup["viewer_token"]

        # Viewers have library:read, should succeed
        response = await async_client.get("/api/v1/library/files", headers={"Authorization": f"Bearer {viewer_token}"})
        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_files_denied_without_permission(self, async_client: AsyncClient, db_session):
        """Verify list_files denied without auth when auth is enabled."""
        from backend.app.models.settings import Settings

        # Enable auth
        settings = Settings(key="auth_enabled", value="true")
        db_session.add(settings)
        await db_session.commit()

        # Request without token should fail. async_client carries a default
        # admin bearer token from conftest; explicitly clear it to exercise
        # the unauthenticated path.
        response = await async_client.get(
            "/api/v1/library/files",
            headers={"Authorization": ""},
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_file_own_by_owner(self, async_client: AsyncClient, db_session, auth_setup, test_file):
        """Verify operator can delete their own files."""
        from pathlib import Path

        # Create actual file on disk so delete doesn't fail
        from backend.app.core.config import settings as app_settings

        file_path = Path(app_settings.base_dir) / test_file.file_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("test content")

        operator_token = auth_setup["operator_token"]

        response = await async_client.delete(
            f"/api/v1/library/files/{test_file.id}", headers={"Authorization": f"Bearer {operator_token}"}
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_file_own_denied_for_others_file(self, async_client: AsyncClient, db_session, auth_setup):
        """Verify operator cannot delete files owned by others."""
        # Create another operator user with a file
        from sqlalchemy import select

        from backend.app.core.auth import create_access_token
        from backend.app.models.group import Group
        from backend.app.models.library import LibraryFile
        from backend.app.models.user import User

        operator_group = (await db_session.execute(select(Group).where(Group.name == "Operators"))).scalar_one()

        from backend.app.core.auth import get_password_hash as get_pw_hash

        other_user = User(username="other_op", password_hash=get_pw_hash("password"), is_active=True)
        other_user.groups.append(operator_group)
        db_session.add(other_user)
        await db_session.commit()
        await db_session.refresh(other_user)

        # Create file owned by other user
        other_file = LibraryFile(
            filename="other.txt",
            file_path="archive/library/files/other.txt",
            file_type="txt",
            file_size=100,
            created_by_id=other_user.id,
        )
        db_session.add(other_file)
        await db_session.commit()
        await db_session.refresh(other_file)

        # Original operator should not be able to delete it
        operator_token = auth_setup["operator_token"]
        response = await async_client.delete(
            f"/api/v1/library/files/{other_file.id}", headers={"Authorization": f"Bearer {operator_token}"}
        )
        assert response.status_code == 403
        assert "your own files" in response.json()["detail"].lower()

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_delete_file_admin_can_delete_any(self, async_client: AsyncClient, db_session, auth_setup):
        """Verify admin can delete any file."""
        from pathlib import Path

        from backend.app.core.config import settings as app_settings
        from backend.app.models.library import LibraryFile

        # Create file owned by operator
        operator_user = auth_setup["operator_user"]
        lib_file = LibraryFile(
            filename="admin_can_delete.txt",
            file_path="archive/library/files/admin_can_delete.txt",
            file_type="txt",
            file_size=100,
            created_by_id=operator_user.id,
        )
        db_session.add(lib_file)
        await db_session.commit()
        await db_session.refresh(lib_file)

        # Create actual file on disk
        file_path = Path(app_settings.base_dir) / lib_file.file_path
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text("test content")

        # Admin should be able to delete it
        admin_token = auth_setup["admin_token"]
        response = await async_client.delete(
            f"/api/v1/library/files/{lib_file.id}", headers={"Authorization": f"Bearer {admin_token}"}
        )
        assert response.status_code == 200

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_viewer_cannot_delete_files(self, async_client: AsyncClient, db_session, auth_setup, test_file):
        """Verify viewer cannot delete any files."""
        viewer_token = auth_setup["viewer_token"]

        response = await async_client.delete(
            f"/api/v1/library/files/{test_file.id}", headers={"Authorization": f"Bearer {viewer_token}"}
        )
        # Viewers don't have delete_own or delete_all permissions
        assert response.status_code == 403
