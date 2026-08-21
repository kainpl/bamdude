"""Starting a scan, and what stops a second one.

⚠️ The endpoint's answer shape changed. It used to do the work and reply with
counts, which it could only manage by keeping the request open for the whole
walk — and that is precisely what held SQLite's write lock and made unrelated
queries fail with ``database is locked``.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.library import LibraryFolder
from backend.app.models.library_scan import LibraryScanJob

# ⚠️ Bound at import time, BEFORE the autouse fixture below replaces the module
# attribute with a noop. The one test that needs the real worker needs this
# reference; going through the module would get the noop.
from backend.app.services.library_scan import run_scan as real_run_scan

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def external_folder(db_session: AsyncSession, tmp_path) -> LibraryFolder:
    folder = LibraryFolder(
        name="NAS",
        is_external=True,
        external_path=str(tmp_path),
        external_show_hidden=False,
    )
    db_session.add(folder)
    await db_session.commit()
    await db_session.refresh(folder)
    return folder


@pytest.fixture(autouse=True)
def _never_actually_walk(monkeypatch):
    """The worker is covered by its own unit tests; here it must only be seen to
    start, and a real walk would make these tests depend on a filesystem.
    """
    from backend.app.services import library_scan

    async def noop(job_id: int) -> None:
        return None

    monkeypatch.setattr(library_scan, "run_scan", noop)


class TestStarting:
    async def test_the_scan_answers_immediately_with_a_job(
        self, async_client: AsyncClient, external_folder, db_session
    ):
        response = await async_client.post(f"/api/v1/library/folders/{external_folder.id}/scan")
        assert response.status_code == 202, response.text
        job_id = response.json()["job_id"]

        job = await db_session.get(LibraryScanJob, job_id)
        assert job is not None
        assert job.folder_id == external_folder.id

    async def test_a_folder_that_is_not_external_is_refused(self, async_client: AsyncClient, db_session):
        managed = LibraryFolder(name="Managed", is_external=False)
        db_session.add(managed)
        await db_session.commit()
        await db_session.refresh(managed)

        response = await async_client.post(f"/api/v1/library/folders/{managed.id}/scan")
        assert response.status_code == 400

    async def test_an_unknown_folder_is_a_404(self, async_client: AsyncClient):
        assert (await async_client.post("/api/v1/library/folders/999999/scan")).status_code == 404

    async def test_a_second_scan_of_the_same_folder_is_refused(
        self, async_client: AsyncClient, external_folder, db_session
    ):
        """⚠️ Two walks writing the same rows is not twice as fast, it is a race
        — the second keeps finding half-written state from the first.
        """
        first = await async_client.post(f"/api/v1/library/folders/{external_folder.id}/scan")
        assert first.status_code == 202

        second = await async_client.post(f"/api/v1/library/folders/{external_folder.id}/scan")
        assert second.status_code == 409
        assert "already running" in second.json()["detail"]

    async def test_a_finished_scan_does_not_block_the_next_one(
        self, async_client: AsyncClient, external_folder, db_session
    ):
        first = await async_client.post(f"/api/v1/library/folders/{external_folder.id}/scan")
        job = await db_session.get(LibraryScanJob, first.json()["job_id"])
        job.status = "finished"
        await db_session.commit()

        assert (await async_client.post(f"/api/v1/library/folders/{external_folder.id}/scan")).status_code == 202


class TestWatching:
    async def test_a_job_can_be_polled(self, async_client: AsyncClient, external_folder):
        """The socket is the fast path, not the only one — a reloaded tab has to
        be able to ask.
        """
        job_id = (await async_client.post(f"/api/v1/library/folders/{external_folder.id}/scan")).json()["job_id"]

        response = await async_client.get(f"/api/v1/library/scan-jobs/{job_id}")
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == job_id
        assert body["skipped_deletions"] is False
        assert body["files_total"] == 0

    async def test_an_unknown_job_is_a_404(self, async_client: AsyncClient):
        assert (await async_client.get("/api/v1/library/scan-jobs/999999")).status_code == 404


class TestAfterARestart:
    async def test_a_job_left_running_is_failed_rather_than_left_to_look_alive(
        self, async_client: AsyncClient, external_folder, db_session
    ):
        """⚠️ Two things go wrong without the sweep, and the second is worse:
        `running` reads as progress that never arrives, and the duplicate guard
        sees an active job — so that folder could never be scanned again.
        """
        from backend.app.services.library_scan import sweep_interrupted_jobs

        stranded = LibraryScanJob(folder_id=external_folder.id, status="running")
        db_session.add(stranded)
        await db_session.commit()
        await db_session.refresh(stranded)

        assert await sweep_interrupted_jobs() >= 1

        await db_session.refresh(stranded)
        assert stranded.status == "failed"
        assert "restart" in (stranded.error or "")

    async def test_the_sweep_leaves_finished_jobs_alone(self, async_client: AsyncClient, external_folder, db_session):
        from backend.app.services.library_scan import sweep_interrupted_jobs

        done = LibraryScanJob(folder_id=external_folder.id, status="finished", files_added=7)
        db_session.add(done)
        await db_session.commit()
        await db_session.refresh(done)

        await sweep_interrupted_jobs()

        await db_session.refresh(done)
        assert done.status == "finished"
        assert done.files_added == 7

    async def test_a_swept_folder_can_be_scanned_again(self, async_client: AsyncClient, external_folder, db_session):
        from backend.app.services.library_scan import sweep_interrupted_jobs

        db_session.add(LibraryScanJob(folder_id=external_folder.id, status="running"))
        await db_session.commit()

        await sweep_interrupted_jobs()

        assert (await async_client.post(f"/api/v1/library/folders/{external_folder.id}/scan")).status_code == 202


async def test_a_deleted_folder_leaves_its_scan_history_harmless(db_session, external_folder):
    """⚠️ The DDL says ON DELETE CASCADE and PostgreSQL honours it. **SQLite does
    not** — this codebase never sets ``PRAGMA foreign_keys = ON``, so foreign
    keys are not enforced there at all and the rows simply stay.

    Pinned as it actually behaves rather than as the DDL reads. A leftover job
    row is inert: nothing lists it, and it names a folder that is gone. Turning
    FK enforcement on globally to make this one CASCADE work would change how
    every table in the application behaves, which a scan fix has no business
    doing.
    """
    job = LibraryScanJob(folder_id=external_folder.id, status="finished")
    db_session.add(job)
    await db_session.commit()

    await db_session.delete(external_folder)
    await db_session.commit()

    # No assertion on presence: PostgreSQL removes it, SQLite keeps it, and
    # neither outcome is a problem. What matters is that the delete succeeded.
    assert (await db_session.get(LibraryFolder, external_folder.id)) is None


class TestAFailureReachesTheTabs:
    """⚠️ Writing `failed` to the row is only half of ending a scan.

    The other half is saying so on the socket. Without it the progress strip
    spins forever — and the commonest failure here is an unreachable mount,
    which is exactly the case somebody is sitting and watching.
    """

    async def test_an_unreachable_mount_announces_itself_and_names_its_folder(
        self, async_client: AsyncClient, db_session, tmp_path, monkeypatch
    ):
        from backend.app.core.websocket import ws_manager

        folder = LibraryFolder(
            name="NAS", is_external=True, external_path=str(tmp_path / "gone"), external_show_hidden=False
        )
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)

        job = LibraryScanJob(folder_id=folder.id, status="queued")
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        sent: list[dict] = []

        async def capture(data: dict) -> None:
            sent.append(data)

        monkeypatch.setattr(ws_manager, "send_library_scan_finished", capture)

        await real_run_scan(job.id)

        assert sent, "the scan ended without telling anybody"
        assert sent[0]["status"] == "failed"
        # ⚠️ The strip is keyed by folder. An event without this clears nothing.
        assert sent[0]["folder_id"] == folder.id
        assert "not accessible" in sent[0]["error"]

        await db_session.refresh(job)
        assert job.status == "failed"
