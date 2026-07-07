"""Integration tests for Slicer Pipeline runs (#1425, BamDude adaptation).

Slicing itself is a network call to the slicer sidecar — these tests stub
``slice_dispatch.enqueue`` so the run-creation logic is exercised without a live
sidecar. The BamDude two-tier enqueue fanout (``_enqueue_copies``) is tested
directly against the DB so both queue paths (pinned PrintQueueItem vs
model-class AutoQueueItem) are covered.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient


def _pipeline_payload(**overrides) -> dict:
    payload = {
        "name": "Production Batch",
        "description": None,
        "printer_preset": {"source": "local", "id": "1"},
        "process_preset": {"source": "local", "id": "2"},
        "filament_presets": [{"source": "local", "id": "3"}],
        "bed_type": None,
    }
    payload.update(overrides)
    return payload


@pytest.fixture
async def pipeline_factory(async_client: AsyncClient):
    """Create pipelines via the API + optionally set a target printer."""

    async def _make(target_printer_id: int | None = None, **overrides) -> dict:
        resp = await async_client.post("/api/v1/slicer-pipelines/", json=_pipeline_payload(**overrides))
        assert resp.status_code == 201, resp.text
        pipeline = resp.json()
        if target_printer_id is not None:
            put_resp = await async_client.put(
                f"/api/v1/slicer-pipelines/{pipeline['id']}",
                json={"target_kind": "specific_printer", "target_printer_id": target_printer_id},
            )
            assert put_resp.status_code == 200, put_resp.text
            pipeline = put_resp.json()
        return pipeline

    return _make


@pytest.fixture
async def printer_factory(db_session):
    """Insert a Printer row for tests that need a target_printer_id."""
    from backend.app.models.printer import Printer

    counter = [0]

    async def _make(**overrides) -> Printer:
        counter[0] += 1
        defaults = {
            "name": f"X1C #{counter[0]}",
            "serial_number": f"SERIAL{counter[0]:04d}",
            "ip_address": "192.0.2.1",
            "access_code": "ABCD1234",
            "model": "Bambu Lab X1 Carbon",
            "is_active": True,
        }
        defaults.update(overrides)
        printer = Printer(**defaults)
        db_session.add(printer)
        await db_session.commit()
        await db_session.refresh(printer)
        return printer

    return _make


@pytest.fixture
async def library_file_factory(db_session):
    """Insert a LibraryFile row for tests that need a source_library_file_id."""
    from pathlib import Path

    from backend.app.core.config import settings as app_settings
    from backend.app.models.library import LibraryFile

    counter = [0]

    async def _make(**overrides) -> LibraryFile:
        counter[0] += 1
        rel = f"test_pipeline_run_{counter[0]}.3mf"
        abs_path = Path(app_settings.base_dir) / rel
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(b"")
        defaults = {
            "filename": f"cube_{counter[0]}.3mf",
            "file_path": rel,
            "file_type": "3mf",
            "file_size": 0,
            "file_hash": f"hash_{counter[0]}",
            "source_type": "uploaded",
        }
        defaults.update(overrides)
        row = LibraryFile(**defaults)
        db_session.add(row)
        await db_session.commit()
        await db_session.refresh(row)
        return row

    return _make


class TestSlicerPipelineTarget:
    """PUT /slicer-pipelines/{id} accepts the target fields."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_writes_target(self, async_client: AsyncClient, pipeline_factory, printer_factory):
        printer = await printer_factory()
        pipeline = await pipeline_factory()
        resp = await async_client.put(
            f"/api/v1/slicer-pipelines/{pipeline['id']}",
            json={"target_kind": "specific_printer", "target_printer_id": printer.id},
        )
        assert resp.status_code == 200, resp.text
        updated = resp.json()
        assert updated["target_kind"] == "specific_printer"
        assert updated["target_printer_id"] == printer.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_update_target_printer_id_zero_clears(
        self, async_client: AsyncClient, pipeline_factory, printer_factory
    ):
        """Empty-select dropdown sends target_printer_id=0 → backend treats
        as 'clear' rather than referencing printer #0."""
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        resp = await async_client.put(
            f"/api/v1/slicer-pipelines/{pipeline['id']}",
            json={"target_printer_id": 0},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["target_printer_id"] is None


class TestCheckEligibility:
    """POST /slicer-pipelines/{id}/check-eligibility surfaces structured issues."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_no_target_set(self, async_client: AsyncClient, pipeline_factory, library_file_factory):
        pipeline = await pipeline_factory()  # no target set
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
            json={"source_library_file_id": src.id},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is False
        kinds = [i["kind"] for i in body["issues"]]
        assert kinds == ["class_not_set"] or kinds == ["printer_not_set"]

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_printer_disabled(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        printer = await printer_factory(is_active=False)
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        with patch("backend.app.api.routes.pipeline_runs._load_printer_status", new=AsyncMock(return_value=None)):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
                json={"source_library_file_id": src.id},
            )
        assert resp.status_code == 200
        body = resp.json()
        kinds = [i["kind"] for i in body["issues"]]
        assert "printer_disabled" in kinds
        assert "printer_offline" in kinds
        assert body["ok"] is False

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_online_match_clears_issues(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        """AMS slot 0 carries the same canonical type the pipeline's local-tier
        filament preset declares → no blocking issues."""
        from backend.app.models.local_preset import LocalPreset

        preset = LocalPreset(
            name="My PLA",
            preset_type="filament",
            source="manual",
            setting="{}",
            filament_type="PLA",
            default_filament_colour="#FFFFFF",
        )
        db_session.add(preset)
        await db_session.commit()
        await db_session.refresh(preset)

        printer = await printer_factory()
        pipeline = await pipeline_factory(
            target_printer_id=printer.id,
            filament_presets=[{"source": "local", "id": str(preset.id)}],
        )
        src = await library_file_factory()

        live_status = {
            "connected": True,
            "raw_data": {"ams": [{"tray": [{"tray_type": "PLA Basic", "tray_color": "FFFFFFFF"}]}]},
        }
        with patch(
            "backend.app.api.routes.pipeline_runs._load_printer_status",
            new=AsyncMock(return_value=live_status),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
                json={"source_library_file_id": src.id},
            )
        assert resp.status_code == 200
        body = resp.json()
        assert body["ok"] is True
        assert body["issues"] == []
        assert body["target_printer_name"] == printer.name


class TestRunPipeline:
    """POST /slicer-pipelines/{id}/run orchestrates slice + enqueue."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_with_issues_and_no_force_returns_409(
        self, async_client: AsyncClient, pipeline_factory, library_file_factory
    ):
        pipeline = await pipeline_factory()  # no target set
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
            json={"source_library_file_id": src.id},
        )
        assert resp.status_code == 409
        detail = resp.json()["detail"]
        assert detail["ok"] is False
        kinds = [i["kind"] for i in detail["issues"]]
        assert "printer_not_set" in kinds or "class_not_set" in kinds

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_force_with_no_target_still_400(
        self, async_client: AsyncClient, pipeline_factory, library_file_factory
    ):
        """``force=True`` bypasses the 409 but the run endpoint still needs a
        target to enqueue against — the second guard returns 400."""
        pipeline = await pipeline_factory()
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
            json={"source_library_file_id": src.id, "force": True},
        )
        assert resp.status_code == 400

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_creates_run_and_job(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()

        live_status = {"connected": True, "raw_data": {"ams": []}}
        from dataclasses import dataclass

        @dataclass
        class _FakeSliceJob:
            id: int = 9001

        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_library_file_id": src.id},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["pipeline_id"] == pipeline["id"]
        assert body["source_library_file_id"] == src.id
        assert body["copies"] == 1
        assert body["status"] == "queued"
        assert len(body["jobs"]) == 1
        assert body["jobs"][0]["copy_index"] == 0
        assert body["eligibility_overridden"] is False
        assert body["slice_job_id"] == 9001

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_accepts_archive_source(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, db_session
    ):
        """``source_archive_id`` is accepted in place of source_library_file_id."""
        from pathlib import Path

        from backend.app.core.config import settings as app_settings
        from backend.app.models.archive import PrintArchive

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)

        rel = "test_pipeline_archive_source.3mf"
        (Path(app_settings.base_dir) / rel).write_bytes(b"")
        archive = PrintArchive(
            printer_id=printer.id,
            filename="Archive Source.3mf",
            file_path=rel,
            file_size=0,
            source_3mf_path=rel,
        )
        db_session.add(archive)
        await db_session.commit()
        await db_session.refresh(archive)

        from dataclasses import dataclass

        @dataclass
        class _FakeSliceJob:
            id: int = 7777

        live_status = {"connected": True, "raw_data": {"ams": []}}
        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_archive_id": archive.id},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["source_library_file_id"] is None
        assert body["source_archive_id"] == archive.id
        assert body["slice_job_id"] == 7777

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_rejects_no_source(self, async_client: AsyncClient, pipeline_factory, printer_factory):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        resp = await async_client.post(f"/api/v1/slicer-pipelines/{pipeline['id']}/run", json={})
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_rejects_both_sources(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
            json={"source_library_file_id": src.id, "source_archive_id": 99},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_copies_cap_schema_gate(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
            json={"source_library_file_id": src.id, "copies": 9999},
        )
        assert resp.status_code == 422  # schema gate (le=1000)

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_copies_cap_setting_enforced(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        """The pipeline_max_copies setting rejects copies above it (before the
        schema's 1000 ceiling)."""
        from backend.app.api.routes.settings import set_setting

        await set_setting(db_session, "pipeline_max_copies", "5")
        await db_session.commit()

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        live_status = {"connected": True, "raw_data": {"ams": []}}
        with patch(
            "backend.app.api.routes.pipeline_runs._load_printer_status",
            new=AsyncMock(return_value=live_status),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_library_file_id": src.id, "copies": 10},
            )
        assert resp.status_code == 422
        assert "pipeline_max_copies" in resp.text

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_run_copies_3_creates_3_jobs(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        from dataclasses import dataclass

        @dataclass
        class _FakeSliceJob:
            id: int = 5555

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()

        live_status = {"connected": True, "raw_data": {"ams": []}}
        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_library_file_id": src.id, "copies": 3},
            )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["copies"] == 3
        assert len(body["jobs"]) == 3
        assert [j["copy_index"] for j in body["jobs"]] == [0, 1, 2]


class TestClassTargeting:
    """target_kind='printer_class' eligibility breakdown."""

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_class_eligibility_per_printer_breakdown(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        await printer_factory(model="X1C")
        await printer_factory(model="X1C")
        await printer_factory(model="P1S")  # noise
        pipeline = await pipeline_factory()
        put_resp = await async_client.put(
            f"/api/v1/slicer-pipelines/{pipeline['id']}",
            json={
                "target_kind": "printer_class",
                "target_printer_id": 0,
                "target_model_class": "X1C",
                "fanout_strategy": "max_parallel",
            },
        )
        assert put_resp.status_code == 200, put_resp.text
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
            json={"source_library_file_id": src.id},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["target_kind"] == "printer_class"
        assert body["target_model_class"] == "X1C"
        assert len(body["printer_reports"]) == 2
        assert all(r["printer_name"].startswith("X1C") for r in body["printer_reports"])
        assert body["ok"] is False  # AMS empty + no live state → both offline

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_class_eligibility_no_matching_printers(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        await printer_factory(model="P1S")
        pipeline = await pipeline_factory()
        await async_client.put(
            f"/api/v1/slicer-pipelines/{pipeline['id']}",
            json={"target_kind": "printer_class", "target_printer_id": 0, "target_model_class": "X1C"},
        )
        src = await library_file_factory()
        resp = await async_client.post(
            f"/api/v1/slicer-pipelines/{pipeline['id']}/check-eligibility",
            json={"source_library_file_id": src.id},
        )
        body = resp.json()
        assert body["ok"] is False
        assert any(i["kind"] == "no_class_matches" for i in body["issues"])


class TestEnqueueFanout:
    """BamDude two-tier enqueue (``_enqueue_copies``): pinned → PrintQueueItem,
    model-class → AutoQueueItem."""

    async def _make_run_with_jobs(self, db_session, pipeline_id, src_id, n):
        from backend.app.models.pipeline_run import PipelineJob, PipelineRun

        run = PipelineRun(
            pipeline_id=pipeline_id,
            source_library_file_id=src_id,
            copies=n,
            status="dispatching",
        )
        db_session.add(run)
        await db_session.flush()
        jobs = []
        for i in range(n):
            job = PipelineJob(pipeline_run_id=run.id, copy_index=i, status="pending")
            db_session.add(job)
            jobs.append(job)
        await db_session.commit()
        for job in jobs:
            await db_session.refresh(job)
        return run, jobs

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_pinned_printer_creates_print_queue_item(
        self, db_session, pipeline_factory, printer_factory, library_file_factory
    ):
        from sqlalchemy import select

        from backend.app.api.routes.pipeline_runs import _enqueue_copies
        from backend.app.models.print_queue import PrintQueueItem
        from backend.app.models.printer_queue import PrinterQueue

        printer = await printer_factory()
        # Production invariant: every printer has a PrinterQueue.
        db_session.add(PrinterQueue(printer_id=printer.id))
        await db_session.commit()

        pipeline = await pipeline_factory(target_printer_id=printer.id)
        sliced = await library_file_factory()
        _run, jobs = await self._make_run_with_jobs(db_session, pipeline["id"], sliced.id, 1)

        await _enqueue_copies(
            db_session,
            jobs=jobs,
            assignments=[(printer.id, None)],
            sliced_library_file_id=sliced.id,
            creator_user_id=None,
        )
        await db_session.commit()

        rows = (
            (await db_session.execute(select(PrintQueueItem).where(PrintQueueItem.library_file_id == sliced.id)))
            .scalars()
            .all()
        )
        assert len(rows) == 1
        assert rows[0].status == "pending"
        assert rows[0].position >= 1
        assert jobs[0].queue_entry_id == rows[0].id
        assert jobs[0].assigned_printer_id == printer.id
        assert jobs[0].auto_queue_item_id is None
        assert jobs[0].dispatched_at is not None
        # Queue entry belongs to the pinned printer's queue.
        pq = (await db_session.execute(select(PrinterQueue).where(PrinterQueue.id == rows[0].queue_id))).scalar_one()
        assert pq.printer_id == printer.id

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_model_class_creates_auto_queue_item(self, db_session, pipeline_factory, library_file_factory):
        from sqlalchemy import select

        from backend.app.api.routes.pipeline_runs import _enqueue_copies
        from backend.app.models.auto_queue import AutoQueueItem

        pipeline = await pipeline_factory()
        sliced = await library_file_factory()
        _run, jobs = await self._make_run_with_jobs(db_session, pipeline["id"], sliced.id, 2)

        await _enqueue_copies(
            db_session,
            jobs=jobs,
            assignments=[(None, "X1C"), (None, "X1C")],
            sliced_library_file_id=sliced.id,
            creator_user_id=None,
        )
        await db_session.commit()

        autos = (
            (await db_session.execute(select(AutoQueueItem).where(AutoQueueItem.library_file_id == sliced.id)))
            .scalars()
            .all()
        )
        assert len(autos) == 2
        assert all(a.target_model == "X1C" and a.status == "pending" for a in autos)
        assert all(j.auto_queue_item_id is not None for j in jobs)
        assert all(j.queue_entry_id is None for j in jobs)
        assert all(j.assigned_printer_id is None for j in jobs)


class TestRunListAndGet:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_runs_empty(self, async_client: AsyncClient, pipeline_factory):
        pipeline = await pipeline_factory()
        resp = await async_client.get(f"/api/v1/slicer-pipelines/{pipeline['id']}/runs")
        assert resp.status_code == 200
        assert resp.json() == {"runs": [], "total": 0}

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_get_run_404(self, async_client: AsyncClient):
        resp = await async_client.get("/api/v1/pipeline-runs/99999")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_list_all_runs_dashboard_endpoint(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        from backend.app.models.pipeline_run import PipelineRun

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        for i in range(3):
            db_session.add(
                PipelineRun(
                    pipeline_id=pipeline["id"],
                    source_library_file_id=src.id,
                    copies=1,
                    status="completed" if i % 2 == 0 else "failed",
                )
            )
        await db_session.commit()

        resp = await async_client.get("/api/v1/pipeline-runs?limit=10")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 3
        assert len(body["runs"]) == 3
        assert body["runs"][0]["id"] > body["runs"][-1]["id"]

        resp = await async_client.get("/api/v1/pipeline-runs?status=failed")
        body = resp.json()
        assert all(r["status"] == "failed" for r in body["runs"])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_dashboard_filters_by_target_printer(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        from backend.app.models.pipeline_run import PipelineRun

        printer_a = await printer_factory()
        printer_b = await printer_factory()
        pipe_a = await pipeline_factory(target_printer_id=printer_a.id)
        pipe_b = await pipeline_factory(target_printer_id=printer_b.id)
        src = await library_file_factory()
        for pipe in (pipe_a, pipe_a, pipe_b):
            db_session.add(
                PipelineRun(
                    pipeline_id=pipe["id"],
                    source_library_file_id=src.id,
                    copies=1,
                    status="completed",
                )
            )
        await db_session.commit()

        resp = await async_client.get(f"/api/v1/pipeline-runs?target_printer_id={printer_a.id}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert all(r["target_printer_id"] == printer_a.id for r in body["runs"])

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_clear_endpoint_deletes_terminal_runs_only(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        from backend.app.models.pipeline_run import PipelineRun

        printer = await printer_factory()
        pipe = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        for status in ("completed", "failed", "cancelled", "partial_failure", "dispatching", "in_progress"):
            db_session.add(
                PipelineRun(
                    pipeline_id=pipe["id"],
                    source_library_file_id=src.id,
                    copies=1,
                    status=status,
                )
            )
        await db_session.commit()

        resp = await async_client.post("/api/v1/pipeline-runs/clear")
        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"] == 4  # 4 terminal statuses cleared

        survivors = (await async_client.get("/api/v1/pipeline-runs")).json()
        assert survivors["total"] == 2
        assert {r["status"] for r in survivors["runs"]} == {"dispatching", "in_progress"}


class TestCancelAndRetry:
    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_unknown_run_404(self, async_client: AsyncClient):
        resp = await async_client.post("/api/v1/pipeline-runs/99999/cancel")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_marks_queued_run(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory
    ):
        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        live_status = {"connected": True, "raw_data": {"ams": []}}
        from dataclasses import dataclass

        @dataclass
        class _FakeSliceJob:
            id: int = 9001

        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            run_resp = await async_client.post(
                f"/api/v1/slicer-pipelines/{pipeline['id']}/run",
                json={"source_library_file_id": src.id},
            )
        run_id = run_resp.json()["id"]
        cancel_resp = await async_client.post(f"/api/v1/pipeline-runs/{run_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["status"] == "cancelled"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_cancel_terminal_run_is_idempotent(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        from backend.app.models.pipeline_run import PipelineRun

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        run = PipelineRun(
            pipeline_id=pipeline["id"],
            source_library_file_id=src.id,
            copies=1,
            status="completed",
        )
        db_session.add(run)
        await db_session.commit()
        await db_session.refresh(run)
        resp = await async_client.post(f"/api/v1/pipeline-runs/{run.id}/cancel")
        assert resp.status_code == 200
        assert resp.json()["status"] == "completed"  # unchanged

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_deleted_queue_entry_rolls_up_as_cancelled(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        """A PipelineJob whose linked queue entry was deleted from the queue
        page rolls up to ``cancelled`` (else the run sits forever queued)."""
        from backend.app.models.pipeline_run import PipelineJob, PipelineRun

        printer = await printer_factory()
        pipe = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        run = PipelineRun(
            pipeline_id=pipe["id"],
            source_library_file_id=src.id,
            copies=1,
            status="dispatching",
        )
        db_session.add(run)
        await db_session.flush()
        db_session.add(
            PipelineJob(
                pipeline_run_id=run.id,
                copy_index=0,
                queue_entry_id=999999,  # gone — simulates manual delete from queue
                assigned_printer_id=printer.id,
                status="queued",
            )
        )
        await db_session.commit()
        await db_session.refresh(run)

        resp = await async_client.get(f"/api/v1/pipeline-runs/{run.id}")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["jobs"][0]["status"] == "cancelled"
        assert body["status"] == "cancelled"

    @pytest.mark.asyncio
    @pytest.mark.integration
    async def test_retry_failed_creates_child_run(
        self, async_client: AsyncClient, pipeline_factory, printer_factory, library_file_factory, db_session
    ):
        from dataclasses import dataclass

        from backend.app.models.pipeline_run import PipelineJob, PipelineRun

        @dataclass
        class _FakeSliceJob:
            id: int = 6666

        printer = await printer_factory()
        pipeline = await pipeline_factory(target_printer_id=printer.id)
        src = await library_file_factory()
        parent = PipelineRun(
            pipeline_id=pipeline["id"],
            source_library_file_id=src.id,
            copies=3,
            status="partial_failure",
        )
        db_session.add(parent)
        await db_session.flush()
        for idx, status in enumerate(["completed", "failed", "failed"]):
            db_session.add(PipelineJob(pipeline_run_id=parent.id, copy_index=idx, status=status))
        await db_session.commit()
        await db_session.refresh(parent)

        live_status = {"connected": True, "raw_data": {"ams": []}}
        with (
            patch(
                "backend.app.api.routes.pipeline_runs._load_printer_status",
                new=AsyncMock(return_value=live_status),
            ),
            patch(
                "backend.app.services.slice_dispatch.slice_dispatch.enqueue",
                new=AsyncMock(return_value=_FakeSliceJob()),
            ),
        ):
            resp = await async_client.post(f"/api/v1/pipeline-runs/{parent.id}/retry-failed")
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["copies"] == 2  # only the 2 failed copies
        assert body["parent_run_id"] == parent.id
