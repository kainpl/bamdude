"""What a bridge says happened to a job it took."""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.label_device import LabelDevice, LabelJob
from backend.app.models.settings import Settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

INSTALLATION = "11111111-2222-3333-4444-555555555555"


@pytest.fixture
async def labels_enabled(db_session: AsyncSession) -> None:
    db_session.add(Settings(key="device_labels_enabled", value="true"))
    await db_session.commit()


async def _device(db_session: AsyncSession, installation_id: str) -> LabelDevice:
    device = LabelDevice(installation_id=installation_id, enabled=True, density=3)
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)
    return device


@pytest.fixture
async def a_device(db_session: AsyncSession) -> LabelDevice:
    return await _device(db_session, INSTALLATION)


async def _claimed(db_session: AsyncSession, device: LabelDevice) -> LabelJob:
    job = LabelJob(device_id=device.id, width_mm=40, height_mm=20, image_png=b"x", status="claimed", copies=1)
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


@pytest.fixture
async def a_claimed_job(db_session: AsyncSession, a_device: LabelDevice) -> LabelJob:
    return await _claimed(db_session, a_device)


class TestReporting:
    async def test_a_success_is_recorded(self, async_client: AsyncClient, labels_enabled, a_claimed_job, db_session):
        response = await async_client.post(
            f"/api/v1/label-devices/jobs/{a_claimed_job.id}/result?installation_id={INSTALLATION}",
            json={"ok": True},
        )
        assert response.status_code == 204
        await db_session.refresh(a_claimed_job)
        assert a_claimed_job.status == "printed"

    async def test_a_failure_is_recorded_with_its_message(
        self, async_client: AsyncClient, labels_enabled, a_claimed_job, db_session
    ):
        response = await async_client.post(
            f"/api/v1/label-devices/jobs/{a_claimed_job.id}/result?installation_id={INSTALLATION}",
            json={"ok": False, "error": "no cassette"},
        )
        assert response.status_code == 204
        await db_session.refresh(a_claimed_job)
        assert a_claimed_job.status == "failed"
        assert a_claimed_job.error == "no cassette"

    async def test_a_failure_with_no_reason_still_says_so(
        self, async_client: AsyncClient, labels_enabled, a_claimed_job, db_session
    ):
        """A NULL error on a failed job reads as "nobody looked". Saying the
        device did not explain is at least true.
        """
        await async_client.post(
            f"/api/v1/label-devices/jobs/{a_claimed_job.id}/result?installation_id={INSTALLATION}",
            json={"ok": False},
        )
        await db_session.refresh(a_claimed_job)
        assert a_claimed_job.error

    async def test_a_success_clears_an_error_from_an_earlier_attempt(
        self, async_client: AsyncClient, labels_enabled, a_device, db_session
    ):
        """Otherwise a job that printed on its second try still reads as broken."""
        job = LabelJob(
            device_id=a_device.id,
            width_mm=40,
            height_mm=20,
            image_png=b"x",
            status="claimed",
            attempts=1,
            error="printer was busy",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        await async_client.post(
            f"/api/v1/label-devices/jobs/{job.id}/result?installation_id={INSTALLATION}",
            json={"ok": True},
        )
        await db_session.refresh(job)
        assert job.status == "printed"
        assert job.error is None


class TestWhoMayReport:
    async def test_a_device_cannot_report_on_another_devices_job(
        self, async_client: AsyncClient, labels_enabled, a_claimed_job, db_session
    ):
        """⚠️ Resolved by device and id together, so this is a 404 rather than a
        permission error — the latter would confirm the job exists, which is
        more than the caller is entitled to know.
        """
        other = await _device(db_session, "some-other-bridge")
        assert other.id != a_claimed_job.device_id

        response = await async_client.post(
            f"/api/v1/label-devices/jobs/{a_claimed_job.id}/result?installation_id=some-other-bridge",
            json={"ok": True},
        )
        assert response.status_code == 404

    async def test_an_unknown_installation_is_a_404_not_a_500(
        self, async_client: AsyncClient, labels_enabled, a_claimed_job
    ):
        response = await async_client.post(
            f"/api/v1/label-devices/jobs/{a_claimed_job.id}/result?installation_id=never-seen",
            json={"ok": True},
        )
        assert response.status_code == 404

    async def test_an_unknown_job_is_a_404(self, async_client: AsyncClient, labels_enabled, a_device):
        response = await async_client.post(
            f"/api/v1/label-devices/jobs/999999/result?installation_id={INSTALLATION}",
            json={"ok": True},
        )
        assert response.status_code == 404


class TestAfterTheSweeper:
    async def test_a_report_on_a_requeued_job_is_accepted_rather_than_refused(
        self, async_client: AsyncClient, labels_enabled, a_device, db_session
    ):
        """⚠️ The sweeper may have requeued a job while the bridge was printing
        it perfectly well. Answering 409 to a device that just did the work
        would leave it retrying something that already came out.
        """
        job = LabelJob(device_id=a_device.id, width_mm=40, height_mm=20, image_png=b"x", status="queued", attempts=1)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await async_client.post(
            f"/api/v1/label-devices/jobs/{job.id}/result?installation_id={INSTALLATION}",
            json={"ok": True},
        )
        assert response.status_code == 204
        await db_session.refresh(job)
        assert job.status == "printed"
