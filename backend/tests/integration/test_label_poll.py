"""The bridge's poll: reporting state, being adopted, and claiming work.

The requests here go out as the authenticated admin client, which holds the
poll permission like any bridge key would. That a *key* is confined to
``can_print_labels`` and reaches nothing else is asserted in
``test_label_device_models.py`` — it is a property of the scope map, not of a
request, and proving it here would only re-test the auth layer.
"""

from __future__ import annotations

import asyncio
import base64

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.label_device import LabelCassette, LabelDevice, LabelJob
from backend.app.models.settings import Settings

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

REPORT = {
    "installation_id": "11111111-2222-3333-4444-555555555555",
    "driver": "niimbot",
    "model": "B1",
    "protocol_version": 3,
    "transport": "serial",
    "address": "COM3",
    "app_version": "0.2.0",
    "paper_state": 1,
    "power_level": 3,
    "printer_reachable": True,
}


@pytest.fixture
async def labels_enabled(db_session: AsyncSession) -> None:
    db_session.add(Settings(key="device_labels_enabled", value="true"))
    await db_session.commit()


@pytest.fixture
async def a_device(db_session: AsyncSession) -> LabelDevice:
    device = LabelDevice(
        installation_id=REPORT["installation_id"],
        driver="niimbot",
        model="B1",
        enabled=True,
        density=3,
        cassette_width_mm=40.0,
        cassette_height_mm=20.0,
        printer_reachable=True,
    )
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)
    return device


@pytest.fixture
async def a_queued_job(db_session: AsyncSession, a_device: LabelDevice) -> LabelJob:
    job = LabelJob(
        device_id=a_device.id,
        width_mm=40.0,
        height_mm=20.0,
        copies=2,
        image_png=b"\x89PNG\r\n\x1a\npretend",
        status="queued",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


class TestIntroducingItself:
    async def test_a_first_poll_creates_a_device_that_is_not_yet_enabled(
        self, async_client: AsyncClient, labels_enabled, db_session
    ):
        """⚠️ Pairing, not trust. Authenticating proves it is a bridge; it does
        not decide that the printer behind it may have our labels.
        """
        response = await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        assert response.status_code == 204

        device = (await db_session.execute(select(LabelDevice))).scalars().one()
        assert device.enabled is False
        assert device.model == "B1"
        assert device.transport == "serial"

    async def test_a_pending_device_still_refreshes_its_liveness(
        self, async_client: AsyncClient, labels_enabled, db_session
    ):
        """ "Alive and waiting for approval" must be distinguishable from "gone",
        and the way it stays distinguishable is that the poll keeps succeeding.
        """
        await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        device = (await db_session.execute(select(LabelDevice))).scalars().one()
        assert device.last_seen_at is not None

    async def test_a_second_poll_does_not_create_a_second_device(
        self, async_client: AsyncClient, labels_enabled, db_session
    ):
        await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        assert len((await db_session.execute(select(LabelDevice))).scalars().all()) == 1

    async def test_a_bridge_cannot_adopt_itself(self, async_client: AsyncClient, labels_enabled, db_session):
        """⚠️ ``enabled`` is not in the report schema at all, so a bridge that
        tries is ignored rather than obeyed.
        """
        await async_client.post("/api/v1/label-devices/poll", json={**REPORT, "enabled": True, "name": "Mine"})
        device = (await db_session.execute(select(LabelDevice))).scalars().one()
        assert device.enabled is False
        assert device.name is None


class TestTheWriteThrottle:
    async def test_last_seen_is_not_rewritten_on_every_single_poll(
        self, async_client: AsyncClient, labels_enabled, a_device, db_session
    ):
        """Polling every few seconds must not mean a database write every few
        seconds, forever.
        """
        await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        await db_session.refresh(a_device)
        first = a_device.last_seen_at

        await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        await db_session.refresh(a_device)
        assert a_device.last_seen_at == first

    async def test_a_changed_report_is_written_through_immediately(
        self, async_client: AsyncClient, labels_enabled, a_device, db_session
    ):
        """⚠️ The throttle is on the clock, not on the content — a cassette swap
        or a printer going away must show up at once.
        """
        await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        await async_client.post("/api/v1/label-devices/poll", json={**REPORT, "printer_reachable": False})
        await db_session.refresh(a_device)
        assert a_device.printer_reachable is False


class TestClaimingWork:
    async def test_an_enabled_device_gets_its_queued_job(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job
    ):
        response = await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        assert response.status_code == 200
        body = response.json()
        assert body["job_id"] == a_queued_job.id
        assert body["copies"] == 2
        assert body["density"] == a_device.density
        assert base64.b64decode(body["image_png"]) == a_queued_job.image_png

    async def test_the_handout_says_how_many_dots_that_is(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job
    ):
        """The device works in dots and the queue works in millimetres; doing
        the conversion in two places is how they come to disagree.
        """
        body = (await async_client.post("/api/v1/label-devices/poll", json=REPORT)).json()
        assert (body["width_px"], body["height_px"]) == (320, 160)

    async def test_a_claimed_job_is_not_handed_out_again(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job
    ):
        assert (await async_client.post("/api/v1/label-devices/poll", json=REPORT)).status_code == 200
        assert (await async_client.post("/api/v1/label-devices/poll", json=REPORT)).status_code == 204

    async def test_a_job_is_handed_out_exactly_once_under_concurrency(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job
    ):
        """⚠️ A bridge that retries a request whose response it never saw must
        not be given the same job twice — which a SELECT-then-UPDATE would.
        """
        results = await asyncio.gather(
            *[async_client.post("/api/v1/label-devices/poll", json=REPORT) for _ in range(5)]
        )
        assert sum(1 for r in results if r.status_code == 200) == 1

    async def test_a_device_nobody_adopted_gets_no_work(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job, db_session
    ):
        a_device.enabled = False
        await db_session.commit()
        assert (await async_client.post("/api/v1/label-devices/poll", json=REPORT)).status_code == 204

    async def test_a_job_waits_while_the_printer_reports_no_paper(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job, db_session
    ):
        """Paper is a ten-second fix, so the job waits rather than failing."""
        response = await async_client.post("/api/v1/label-devices/poll", json={**REPORT, "paper_state": 0})
        assert response.status_code == 204
        await db_session.refresh(a_queued_job)
        assert a_queued_job.status == "queued"

    async def test_a_device_that_says_nothing_about_paper_is_not_starved(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job
    ):
        """⚠️ ``None`` is "did not say", which is not "has none". Treating the
        two alike would let a driver that reports no paper state print nothing,
        forever, with no error anywhere.
        """
        response = await async_client.post("/api/v1/label-devices/poll", json={**REPORT, "paper_state": None})
        assert response.status_code == 200

    async def test_a_job_waits_while_the_printer_itself_is_unreachable(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job
    ):
        """The bridge is up — that is why we are talking — but the cable is out.
        Handing it a job would burn an attempt on a certainty.
        """
        response = await async_client.post("/api/v1/label-devices/poll", json={**REPORT, "printer_reachable": False})
        assert response.status_code == 204


class TestTheCadence:
    async def test_every_answer_tells_the_bridge_when_to_come_back(
        self, async_client: AsyncClient, labels_enabled, a_device
    ):
        """The cadence is the server's to set — an administrator can slow a
        chatty bridge down from where they already are.
        """
        response = await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        assert response.status_code == 204
        assert int(response.headers["retry-after"]) >= 1

    async def test_a_delivered_job_says_come_back_immediately(
        self, async_client: AsyncClient, labels_enabled, a_device, a_queued_job
    ):
        """So a batch of ten labels drains at printer speed rather than one per
        poll interval.
        """
        response = await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        assert response.status_code == 200
        assert response.headers["retry-after"] == "0"

    async def test_a_disabled_subsystem_says_come_back_much_later(self, async_client: AsyncClient):
        """The answer cannot change until somebody visits a settings page."""
        response = await async_client.post("/api/v1/label-devices/poll", json=REPORT)
        assert response.status_code == 409
        assert int(response.headers["retry-after"]) >= 60


class TestTheCassette:
    async def test_an_unknown_barcode_leaves_the_size_unresolved(
        self, async_client: AsyncClient, labels_enabled, db_session
    ):
        """⚠️ No call to any vendor cloud. A self-hosted install does not quietly
        send consumable identifiers to a third party — the catalogue is taught.
        """
        await async_client.post(
            "/api/v1/label-devices/poll",
            json={**REPORT, "cassette": {"barcode": "6972842748577"}},
        )
        device = (await db_session.execute(select(LabelDevice))).scalars().one()
        assert device.cassette_barcode == "6972842748577"
        assert device.cassette_width_mm is None

    async def test_teaching_a_barcode_resolves_it_on_the_next_poll(
        self, async_client: AsyncClient, labels_enabled, db_session
    ):
        assert (
            await async_client.put(
                "/api/v1/label-cassettes/6972842748577",
                json={"width_mm": 50, "height_mm": 30, "name": "50 x 30 white"},
            )
        ).status_code == 200

        await async_client.post(
            "/api/v1/label-devices/poll",
            json={**REPORT, "cassette": {"barcode": "6972842748577"}},
        )
        device = (await db_session.execute(select(LabelDevice))).scalars().one()
        assert (device.cassette_width_mm, device.cassette_height_mm) == (50, 30)

    async def test_forgetting_a_cassette_leaves_the_device_unresolved_again(
        self, async_client: AsyncClient, labels_enabled, db_session
    ):
        db_session.add(LabelCassette(barcode="abc123", width_mm=40, height_mm=20))
        await db_session.commit()

        await async_client.post("/api/v1/label-devices/poll", json={**REPORT, "cassette": {"barcode": "abc123"}})
        assert (await async_client.delete("/api/v1/label-cassettes/abc123")).status_code == 204

        await async_client.post("/api/v1/label-devices/poll", json={**REPORT, "cassette": {"barcode": "abc123"}})
        device = (await db_session.execute(select(LabelDevice))).scalars().one()
        assert device.cassette_width_mm is None

    async def test_forgetting_one_that_was_never_taught_is_a_404(self, async_client: AsyncClient, labels_enabled):
        assert (await async_client.delete("/api/v1/label-cassettes/never-seen")).status_code == 404
