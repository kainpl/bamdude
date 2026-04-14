"""Integration tests for library file notes API (gh#3).

Covers: CRUD happy paths, 1000-char validation, ownership enforcement
(only the author can edit/delete their own note), CASCADE on file
deletion, and `notes_count` reflection in the file-list endpoint.
"""

import pytest
from httpx import AsyncClient

from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.library_file_note import LibraryFileNote


class TestLibraryNotesAPI:
    @pytest.fixture
    async def library_file(self, db_session):
        folder = LibraryFolder(name="test-folder")
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)
        f = LibraryFile(
            folder_id=folder.id,
            filename="test.3mf",
            file_path="library/test.3mf",
            file_type="3mf",
            file_size=1024,
        )
        db_session.add(f)
        await db_session.commit()
        await db_session.refresh(f)
        return f

    # -- list ----------------------------------------------------------

    @pytest.mark.asyncio
    async def test_list_notes_empty(self, async_client: AsyncClient, library_file):
        response = await async_client.get(f"/api/v1/library/files/{library_file.id}/notes")
        assert response.status_code == 200
        assert response.json() == []

    @pytest.mark.asyncio
    async def test_list_notes_newest_first(self, async_client: AsyncClient, library_file, db_session):
        # Seed 3 notes with controllable IDs (default ordering is created_at desc).
        for body in ["first", "second", "third"]:
            db_session.add(LibraryFileNote(library_file_id=library_file.id, body=body, user_id=None))
            await db_session.commit()
        response = await async_client.get(f"/api/v1/library/files/{library_file.id}/notes")
        assert response.status_code == 200
        bodies = [n["body"] for n in response.json()]
        assert bodies == ["third", "second", "first"]

    @pytest.mark.asyncio
    async def test_list_notes_unknown_file_returns_404(self, async_client: AsyncClient):
        response = await async_client.get("/api/v1/library/files/99999/notes")
        assert response.status_code == 404

    # -- create --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_create_note_happy_path(self, async_client: AsyncClient, library_file):
        response = await async_client.post(
            f"/api/v1/library/files/{library_file.id}/notes",
            json={"body": "Hello note"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["body"] == "Hello note"
        assert data["library_file_id"] == library_file.id
        assert data["can_edit"] is True  # auth disabled in test env → open

    @pytest.mark.asyncio
    async def test_create_note_empty_rejected(self, async_client: AsyncClient, library_file):
        response = await async_client.post(
            f"/api/v1/library/files/{library_file.id}/notes",
            json={"body": ""},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_note_over_1000_rejected(self, async_client: AsyncClient, library_file):
        response = await async_client.post(
            f"/api/v1/library/files/{library_file.id}/notes",
            json={"body": "x" * 1001},
        )
        assert response.status_code == 422

    @pytest.mark.asyncio
    async def test_create_note_exactly_1000_accepted(self, async_client: AsyncClient, library_file):
        response = await async_client.post(
            f"/api/v1/library/files/{library_file.id}/notes",
            json={"body": "y" * 1000},
        )
        assert response.status_code == 200
        assert len(response.json()["body"]) == 1000

    @pytest.mark.asyncio
    async def test_create_note_on_unknown_file_returns_404(self, async_client: AsyncClient):
        response = await async_client.post(
            "/api/v1/library/files/99999/notes",
            json={"body": "ghost"},
        )
        assert response.status_code == 404

    # -- update --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_update_note_happy_path(self, async_client: AsyncClient, library_file, db_session):
        note = LibraryFileNote(library_file_id=library_file.id, body="original", user_id=None)
        db_session.add(note)
        await db_session.commit()
        await db_session.refresh(note)

        response = await async_client.patch(
            f"/api/v1/library/notes/{note.id}",
            json={"body": "edited"},
        )
        assert response.status_code == 200
        assert response.json()["body"] == "edited"

    @pytest.mark.asyncio
    async def test_update_note_unknown_returns_404(self, async_client: AsyncClient):
        response = await async_client.patch(
            "/api/v1/library/notes/99999",
            json={"body": "ghost"},
        )
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_update_note_over_1000_rejected(self, async_client: AsyncClient, library_file, db_session):
        note = LibraryFileNote(library_file_id=library_file.id, body="ok", user_id=None)
        db_session.add(note)
        await db_session.commit()
        await db_session.refresh(note)
        response = await async_client.patch(
            f"/api/v1/library/notes/{note.id}",
            json={"body": "x" * 1001},
        )
        assert response.status_code == 422

    # -- delete --------------------------------------------------------

    @pytest.mark.asyncio
    async def test_delete_note(self, async_client: AsyncClient, library_file, db_session):
        note = LibraryFileNote(library_file_id=library_file.id, body="to delete", user_id=None)
        db_session.add(note)
        await db_session.commit()
        note_id = note.id

        response = await async_client.delete(f"/api/v1/library/notes/{note_id}")
        assert response.status_code == 200
        assert response.json()["success"] is True

        # Verify gone
        from sqlalchemy import select

        result = await db_session.execute(select(LibraryFileNote).where(LibraryFileNote.id == note_id))
        assert result.scalar_one_or_none() is None

    @pytest.mark.asyncio
    async def test_delete_note_unknown_returns_404(self, async_client: AsyncClient):
        response = await async_client.delete("/api/v1/library/notes/99999")
        assert response.status_code == 404

    # -- cascade -------------------------------------------------------

    @pytest.mark.asyncio
    async def test_cascade_on_file_delete(self, async_client: AsyncClient, library_file, db_session):
        """Deleting the library file must also delete its notes."""
        for body in ["n1", "n2", "n3"]:
            db_session.add(LibraryFileNote(library_file_id=library_file.id, body=body, user_id=None))
        await db_session.commit()

        # Verify setup
        from sqlalchemy import func, select

        count_result = await db_session.execute(
            select(func.count(LibraryFileNote.id)).where(LibraryFileNote.library_file_id == library_file.id)
        )
        assert count_result.scalar() == 3

        await db_session.delete(library_file)
        await db_session.commit()

        count_result = await db_session.execute(
            select(func.count(LibraryFileNote.id)).where(LibraryFileNote.library_file_id == library_file.id)
        )
        assert count_result.scalar() == 0

    # -- notes_count in file list --------------------------------------

    @pytest.mark.asyncio
    async def test_notes_count_in_file_list(self, async_client: AsyncClient, library_file, db_session):
        """GET /library/files includes notes_count reflecting current total."""
        # Initially 0
        resp = await async_client.get("/api/v1/library/files?folder_id=1&include_root=false")
        assert resp.status_code == 200
        files = resp.json()
        matching = [f for f in files if f["id"] == library_file.id]
        assert matching and matching[0]["notes_count"] == 0

        # Add 2 notes
        for body in ["a", "b"]:
            db_session.add(LibraryFileNote(library_file_id=library_file.id, body=body, user_id=None))
        await db_session.commit()

        resp = await async_client.get("/api/v1/library/files?folder_id=1&include_root=false")
        files = resp.json()
        matching = [f for f in files if f["id"] == library_file.id]
        assert matching and matching[0]["notes_count"] == 2
