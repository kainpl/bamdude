"""Enqueueing a label for a bridge-attached printer."""

from __future__ import annotations

import io

import pytest
from httpx import AsyncClient
from PIL import Image
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.label_device import LabelDevice, LabelJob
from backend.app.models.label_template import LabelTemplate
from backend.app.models.settings import Settings
from backend.app.models.spool import Spool

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]


@pytest.fixture
async def labels_enabled(db_session: AsyncSession) -> None:
    db_session.add(Settings(key="device_labels_enabled", value="true"))
    await db_session.commit()


@pytest.fixture
async def a_template(db_session: AsyncSession) -> LabelTemplate:
    """40 x 20 mm, the size the device fixtures below report as loaded."""
    row = LabelTemplate(
        name="Shelf tag 40 x 20",
        width_mm=40.0,
        height_mm=20.0,
        shape="rect",
        elements=[
            {
                "type": "text",
                "x_mm": 1.5,
                "y_mm": 1.5,
                "w_mm": 25.0,
                "h_mm": 4.0,
                "content": "{display_name}",
                "size_mm": 4.0,
                "bold": True,
            },
            {"type": "qr", "x_mm": 28.0, "y_mm": 1.5, "w_mm": 10.0, "h_mm": 10.0, "content": "{deeplink}"},
        ],
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


async def _device(db_session: AsyncSession, **overrides) -> LabelDevice:
    fields = {
        "installation_id": "11111111-2222-3333-4444-555555555555",
        "driver": "niimbot",
        "model": "B1",
        "enabled": True,
        "density": 3,
        "cassette_width_mm": 40.0,
        "cassette_height_mm": 20.0,
        "printer_reachable": True,
    }
    fields.update(overrides)
    device = LabelDevice(**fields)
    db_session.add(device)
    await db_session.commit()
    await db_session.refresh(device)
    return device


@pytest.fixture
async def a_device(db_session: AsyncSession) -> LabelDevice:
    return await _device(db_session)


@pytest.fixture
async def a_pending_device(db_session: AsyncSession) -> LabelDevice:
    return await _device(db_session, installation_id="pending-0001", enabled=False)


@pytest.fixture
async def a_device_without_cassette(db_session: AsyncSession) -> LabelDevice:
    return await _device(db_session, installation_id="no-cassette-1", cassette_width_mm=None, cassette_height_mm=None)


@pytest.fixture
async def two_spools(db_session: AsyncSession) -> list[Spool]:
    spools = [
        Spool(material="PLA", brand="Polymaker", color_name="Ivory", label_weight=1000, weight_used=250),
        Spool(material="PETG", brand="Prusament", color_name="Orange", label_weight=1000, weight_used=100),
    ]
    for spool in spools:
        db_session.add(spool)
    await db_session.commit()
    for spool in spools:
        await db_session.refresh(spool)
    return spools


class TestTheGate:
    async def test_enqueue_is_refused_while_the_subsystem_is_off(self, async_client: AsyncClient, a_device, two_spools):
        """⚠️ A queue nothing will ever drain is worse than an absent feature —
        it looks like it is working.
        """
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        assert response.status_code == 409
        assert "device_labels" in response.json()["detail"]

    async def test_enqueue_is_refused_for_a_device_nobody_adopted(
        self, async_client: AsyncClient, labels_enabled, a_pending_device, two_spools, a_template
    ):
        """Pairing, not trust: the bridge authenticated, which says nothing about
        whether this printer should be given our labels.
        """
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_pending_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        assert response.status_code == 409
        assert "adopted" in response.json()["detail"]


class TestEnqueue:
    async def test_one_queued_job_per_spool_in_the_order_asked_for(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template
    ):
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": s.id} for s in two_spools]},
        )
        assert response.status_code == 201, response.text
        jobs = response.json()
        assert len(jobs) == 2
        assert {j["status"] for j in jobs} == {"queued"}
        assert [j["spool_id"] for j in jobs] == [s.id for s in two_spools]

    async def test_the_raster_is_stored_at_enqueue_not_at_claim(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template, db_session
    ):
        """⚠️ A spool renamed between queueing and printing must not change the
        label that comes out. The queue can sit for hours on a switched-off
        desktop.
        """
        await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        job = (await db_session.execute(select(LabelJob))).scalars().first()
        assert job.image_png[:8] == b"\x89PNG\r\n\x1a\n"

    async def test_the_stored_picture_is_the_size_the_head_will_print(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template, db_session
    ):
        """8 dots per millimetre is 203 dpi — 40 mm is 320 dots."""
        await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        job = (await db_session.execute(select(LabelJob))).scalars().first()
        img = Image.open(io.BytesIO(job.image_png))
        assert (img.width, img.height) == (320, 160)
        assert img.mode == "1"

    async def test_the_job_records_which_design_drew_it(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template
    ):
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}], "template_id": a_template.id},
        )
        assert response.json()[0]["template_id"] == a_template.id

    async def test_copies_ride_on_the_job_rather_than_multiplying_it(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template
    ):
        """The device repeats the same raster; storing it three times would put
        three identical pictures in the database to print one label three times.
        """
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}], "copies": 3},
        )
        assert len(response.json()) == 1
        assert response.json()[0]["copies"] == 3

    async def test_an_unknown_spool_is_a_404(self, async_client: AsyncClient, labels_enabled, a_device, a_template):
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": 999999}]},
        )
        assert response.status_code == 404


class TestWhichDesignAtWhatSize:
    """⚠️ The template is the truth and the cassette is the gate. A design is
    printed at exactly its own size, never scaled to fit — fractional scaling of
    a 1-bit raster destroys the bar-width ratios a scanner reads by, and the
    failure is silent: the label looks fine and will not scan.
    """

    async def test_a_design_is_picked_to_match_the_loaded_stock(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template
    ):
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        assert response.status_code == 201
        assert (response.json()[0]["width_mm"], response.json()[0]["height_mm"]) == (40.0, 20.0)

    async def test_a_design_too_big_for_the_loaded_stock_is_refused(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, db_session
    ):
        big = LabelTemplate(name="Wide", width_mm=74.0, height_mm=33.0, shape="rect", elements=[])
        db_session.add(big)
        await db_session.commit()
        await db_session.refresh(big)

        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}], "template_id": big.id},
        )
        assert response.status_code == 422
        detail = response.json()["detail"]
        assert "74 x 33" in detail and "40 x 20" in detail

    async def test_a_device_that_has_reported_no_stock_is_asked_rather_than_guessed_at(
        self, async_client: AsyncClient, labels_enabled, a_device_without_cassette, two_spools, a_template
    ):
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device_without_cassette.id, "spools": [{"id": two_spools[0].id}]},
        )
        assert response.status_code == 422
        assert "template_id" in response.json()["detail"]

    async def test_but_naming_a_design_works_without_a_known_cassette(
        self, async_client: AsyncClient, labels_enabled, a_device_without_cassette, two_spools, a_template
    ):
        """The gate cannot judge what it has not been told; refusing here would
        make an un-taught barcode block printing entirely.
        """
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={
                "device_id": a_device_without_cassette.id,
                "spools": [{"id": two_spools[0].id}],
                "template_id": a_template.id,
            },
        )
        assert response.status_code == 201

    async def test_no_design_of_that_size_is_a_refusal_naming_the_size(
        self, async_client: AsyncClient, labels_enabled, two_spools, db_session
    ):
        device = await _device(db_session, installation_id="odd-stock", cassette_width_mm=12.0, cassette_height_mm=9.0)
        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": device.id, "spools": [{"id": two_spools[0].id}]},
        )
        assert response.status_code == 422
        assert "12 x 9" in response.json()["detail"]

    async def test_a_design_somebody_made_beats_a_builtin_of_the_same_size(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template, db_session
    ):
        """If they drew one for this stock, that is the one they mean."""
        builtin = LabelTemplate(
            name="AAA sorts first", width_mm=40.0, height_mm=20.0, shape="rect", elements=[], builtin_key="box_40x20"
        )
        db_session.add(builtin)
        await db_session.commit()

        response = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        assert response.status_code == 201


class TestPreview:
    async def test_preview_renders_without_queueing(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template, db_session
    ):
        response = await async_client.post(
            "/api/v1/label-jobs/preview",
            json={"device_id": a_device.id, "spool": {"id": two_spools[0].id}},
        )
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/png"
        assert (await db_session.execute(select(func.count(LabelJob.id)))).scalar() == 0

    async def test_the_preview_is_the_picture_the_device_would_get(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template, db_session
    ):
        """⚠️ Same code, same resolution. A preview drawn any other way is a
        second implementation of the label, and it would disagree.
        """
        preview = await async_client.post(
            "/api/v1/label-jobs/preview",
            json={"device_id": a_device.id, "spool": {"id": two_spools[0].id}},
        )
        await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        job = (await db_session.execute(select(LabelJob))).scalars().first()
        assert preview.content == job.image_png


class TestCancel:
    async def test_a_queued_job_can_be_cancelled(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template
    ):
        created = await async_client.post(
            "/api/v1/label-jobs",
            json={"device_id": a_device.id, "spools": [{"id": two_spools[0].id}]},
        )
        job_id = created.json()[0]["id"]
        assert (await async_client.delete(f"/api/v1/label-jobs/{job_id}")).status_code == 204

    async def test_a_claimed_one_cannot(self, async_client: AsyncClient, labels_enabled, a_device, db_session):
        """The paper is already moving. Deleting the row from under a bridge that
        is about to report on it turns one wasted label into a confusing error.
        """
        job = LabelJob(device_id=a_device.id, width_mm=40, height_mm=20, image_png=b"x", status="claimed", copies=1)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await async_client.delete(f"/api/v1/label-jobs/{job.id}")
        assert response.status_code == 409
        assert "claimed" in response.json()["detail"]


class TestTheNameOnTheLabel:
    async def test_the_naming_setting_is_applied_server_side(
        self, async_client: AsyncClient, labels_enabled, a_device, two_spools, a_template, db_session
    ):
        """The same rule the PDF path follows, so one spool does not get two
        different labels depending on which button was pressed.
        """
        db_session.add(Settings(key="spool_display_template", value="{brand} {material}"))
        await db_session.commit()

        first = await async_client.post(
            "/api/v1/label-jobs/preview",
            json={"device_id": a_device.id, "spool": {"id": two_spools[0].id}},
        )
        second = await async_client.post(
            "/api/v1/label-jobs/preview",
            json={"device_id": a_device.id, "spool": {"id": two_spools[0].id, "display_name": "Shelf B"}},
        )
        assert first.status_code == second.status_code == 200
        assert first.content != second.content, "the override must reach the raster"


class TestTestPrint:
    """Putting the design on screen onto real stock.

    ⚠️ The point is that it takes the SAME route as a real print — same gate,
    same renderer, same queue. A test that took a private path would prove
    nothing about the print it is meant to rehearse.
    """

    def _design(self, width: float = 40.0, height: float = 20.0) -> dict:
        return {
            "name": "Work in progress",
            "width_mm": width,
            "height_mm": height,
            "shape": "rect",
            "elements": [
                {
                    "type": "text",
                    "x_mm": 1.5,
                    "y_mm": 1.5,
                    "w_mm": 25.0,
                    "h_mm": 4.0,
                    "content": "{display_name}",
                    "size_mm": 4.0,
                }
            ],
        }

    async def test_an_unsaved_design_can_be_printed(
        self, async_client: AsyncClient, labels_enabled, a_device, db_session
    ):
        """The whole point: checking a design before committing to it means
        before saving it.
        """
        response = await async_client.post(
            "/api/v1/label-templates/test-print",
            json={"device_id": a_device.id, "template": self._design()},
        )
        assert response.status_code == 201, response.text
        assert response.json()["job_id"]
        assert (await db_session.execute(select(func.count(LabelTemplate.id)))).scalar() == 0

    async def test_it_is_queued_like_any_other_label(
        self, async_client: AsyncClient, labels_enabled, a_device, db_session
    ):
        await async_client.post(
            "/api/v1/label-templates/test-print",
            json={"device_id": a_device.id, "template": self._design()},
        )
        job = (await db_session.execute(select(LabelJob))).scalars().one()
        assert job.status == "queued"
        assert job.device_id == a_device.id
        assert job.image_png[:8] == b"\x89PNG\r\n\x1a\n"

    async def test_it_belongs_to_no_spool(self, async_client: AsyncClient, labels_enabled, a_device, db_session):
        """⚠️ Nothing was printed *about* a spool. Recording one would put a
        test label in that spool's history.
        """
        await async_client.post(
            "/api/v1/label-templates/test-print",
            json={"device_id": a_device.id, "template": self._design()},
        )
        job = (await db_session.execute(select(LabelJob))).scalars().one()
        assert job.spool_id is None
        assert job.template_id is None

    async def test_a_design_too_big_for_the_stock_is_refused_here_too(
        self, async_client: AsyncClient, labels_enabled, a_device
    ):
        """⚠️ The same gate as the real print. A test that succeeded where the
        real one refuses is worse than no test at all.
        """
        response = await async_client.post(
            "/api/v1/label-templates/test-print",
            json={"device_id": a_device.id, "template": self._design(74.0, 33.0)},
        )
        assert response.status_code == 422
        assert "does not fit" in response.json()["detail"]

    async def test_it_is_refused_while_the_subsystem_is_off(self, async_client: AsyncClient, a_device):
        response = await async_client.post(
            "/api/v1/label-templates/test-print",
            json={"device_id": a_device.id, "template": self._design()},
        )
        assert response.status_code == 409

    async def test_it_is_refused_for_a_device_nobody_adopted(
        self, async_client: AsyncClient, labels_enabled, a_pending_device
    ):
        response = await async_client.post(
            "/api/v1/label-templates/test-print",
            json={"device_id": a_pending_device.id, "template": self._design()},
        )
        assert response.status_code == 409
        assert "adopted" in response.json()["detail"]

    async def test_the_picture_matches_what_the_editor_previews(
        self, async_client: AsyncClient, labels_enabled, a_device, db_session
    ):
        """⚠️ Example data on both sides, so what comes out of the printer is
        what the screen was showing — not a different label using the same
        design.
        """
        preview = await async_client.post(
            "/api/v1/label-templates/preview",
            json={"template": self._design(), "dots_per_mm": 8.0},
        )
        await async_client.post(
            "/api/v1/label-templates/test-print",
            json={"device_id": a_device.id, "template": self._design()},
        )
        job = (await db_session.execute(select(LabelJob))).scalars().one()
        assert preview.content == job.image_png
