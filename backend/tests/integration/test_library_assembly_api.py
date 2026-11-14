""" "Assemble project" end to end: the routes, the job, the real packing microservice, the library ingest."""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy import select

from backend.app.core.config import settings as app_settings
from backend.app.models.library import LibraryFile, LibraryFolder
from backend.app.models.local_preset import LocalPreset
from backend.app.services import orca_profiles
from backend.app.services.assembly import job as assembly_job, runtime as assembly_runtime
from backend.app.services.assembly.runtime import AssemblyRuntime
from backend.app.services.local_worker_broker import LocalWorkerBroker
from backend.app.services.slice_dispatch import slice_dispatch
from backend.tests.unit.test_assembly_child import SHAPES, _stl

API = "/api/v1"


def _square(size: int) -> str:
    return f"0x0,{size}x0,{size}x{size},0x{size}"


def _area(x0, y0, x1, y1) -> str:
    return f"{x0}x{y0},{x1}x{y0},{x1}x{y1},{x0}x{y1}"


@pytest_asyncio.fixture
async def farm(db_session, tmp_path, monkeypatch):
    """A library folder with models on disk, local presets, and the real assembly service."""
    monkeypatch.setattr(app_settings, "base_dir", tmp_path, raising=False)
    monkeypatch.setattr(app_settings, "archive_dir", tmp_path / "archive", raising=False)
    (tmp_path / "archive").mkdir()
    storage = tmp_path / "library" / "files"
    storage.mkdir(parents=True)
    folder = LibraryFolder(name="parts")
    db_session.add(folder)
    await db_session.flush()

    files = {}
    for name, shape, suffix in [
        ("frame", "frame", "stl"),
        ("lid", "lid", "stl"),
        ("big", "big200", "stl"),
        ("drawing", "lid", "step"),
        ("project", "lid", "3mf"),
    ]:
        data = _stl(SHAPES[shape])
        path = storage / f"{name}.{suffix}"
        path.write_bytes(data)
        row = LibraryFile(
            folder_id=folder.id,
            filename=f"{name}.{suffix}",
            file_path=str(path.relative_to(tmp_path)),
            file_type=suffix,
            file_size=len(data),
            file_hash=hashlib.sha256(data).hexdigest(),
        )
        db_session.add(row)
        files[name] = row

    presets = {}
    for key, kind, setting in [
        (
            "x1",
            "printer",
            {"printable_area": _square(256), "printable_height": "250", "printer_model": "Bambu Lab X1 Carbon"},
        ),
        (
            "small",
            "printer",
            {"printable_area": _square(180), "printable_height": "180", "printer_model": "Bambu Lab A1 mini"},
        ),
        (
            "dual",
            "printer",
            {
                "printable_area": _square(350),
                "printable_height": "320",
                "printer_model": "Bambu Lab H2D",
                "extruder_printable_area": [_area(0, 0, 325, 320), _area(25, 0, 350, 320)],
                "extruder_printable_height": ["320", "325"],
            },
        ),
        ("ghost", "printer", {"inherits": "A parent nobody has", "printable_height": "250"}),
        (
            "centred",
            "printer",
            {
                "printable_area": ["-110x-110", "110x-110", "110x110", "-110x110"],
                "printable_height": "200",
                "printer_model": "Custom Delta",
            },
        ),
        ("process", "process", {"layer_height": "0.2"}),
        ("filament", "filament", {"filament_type": ["PLA"], "filament_id": "GFL99"}),
    ]:
        name = f"Test {key}"
        preset = LocalPreset(
            name=name,
            preset_type=kind,
            source="orcaslicer",
            setting=json.dumps({"name": name, "type": kind, **setting}),
        )
        db_session.add(preset)
        presets[key] = preset
    await db_session.commit()

    class _NoNetwork:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise orca_profiles.httpx.ConnectError("no network in tests")

    monkeypatch.setattr(orca_profiles.httpx, "AsyncClient", _NoNetwork)
    monkeypatch.setattr(assembly_runtime, "_POLL_SECONDS", 0.1)
    broker = LocalWorkerBroker(tmp_path / ".cache" / "preview-service")
    await broker.start()
    service = AssemblyRuntime(tmp_path, broker)
    await service.start()
    assert service.ready
    monkeypatch.setattr(assembly_runtime, "runtime", service)
    try:
        yield {
            "folder_id": folder.id,
            "files": {name: row.id for name, row in files.items()},
            "presets": {key: preset.id for key, preset in presets.items()},
            "runtime": service,
            "tmp": tmp_path,
        }
    finally:
        await slice_dispatch.quiesce_for_restore(20)
        slice_dispatch.open = True
        await service.stop()
        await broker.stop()


def _row(farm, printer="x1", nozzle=1) -> dict:
    ref = lambda key: {"source": "local", "id": str(farm["presets"][key])}  # noqa: E731
    return {
        "printer_preset": ref(printer),
        "process_preset": ref("process"),
        "filament_preset": ref("filament"),
        "nozzle": nozzle,
    }


def _body(farm, parts, printers=("x1",), name="jar", **layout) -> dict:
    return {
        "parts": [{"file_id": farm["files"][name_], **options} for name_, options in parts],
        "targets": [_row(farm, printer) for printer in printers],
        "layout": layout,
        "name": name,
    }


async def _finish(client: AsyncClient, response, timeout: float = 90.0) -> dict:
    """Wait for the job's own task, then read its terminal state once (see test_slice_api._wait_for_job)."""
    assert response.status_code == 202, response.text
    job_id = response.json()["job_id"]
    task = slice_dispatch._tasks.get(job_id)
    if task is not None:
        await asyncio.wait({task}, timeout=timeout)
        assert task.done(), f"assembly job {job_id} did not finish in {timeout}s"
    body = (await client.get(f"{API}/slice-jobs/{job_id}")).json()
    assert body["status"] in ("completed", "failed"), body
    return body


async def _assembled_files(db) -> list[LibraryFile]:
    db.expire_all()
    rows = (await db.scalars(LibraryFile.active().where(LibraryFile.source_type == "assembled"))).all()
    return sorted(rows, key=lambda row: row.id)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_assemble_two_files_for_one_printer(async_client, db_session, farm):
    response = await async_client.post(
        f"{API}/library/assemble", json=_body(farm, [("frame", {}), ("lid", {"quantity": 3})])
    )
    job = await _finish(async_client, response)
    assert job["status"] == "completed" and job["kind"] == "assembly", job
    result = job["result"]
    printer = result["printers"][0]
    assert result["outcome"] == "ok" and printer["status"] == "ok" and printer["label"] == "Test x1"
    assert len(printer["files"]) == 1 and printer["files"][0]["plate"] is None
    assert printer["plates"][0]["counts"] == {"0": 1, "1": 3}
    assert printer["plates"][0]["picture"].startswith("data:image/png;base64,")

    (made,) = await _assembled_files(db_session)
    assert made.id == printer["files"][0]["library_file_id"] and made.folder_id == farm["folder_id"]
    assert made.filename.startswith("jar — ") and made.filename.endswith(".3mf")
    assert made.file_metadata["assembled"] is True
    detail = (await async_client.get(f"{API}/library/files/{made.id}")).json()
    assert detail["can_relayout"] is True and detail["source_type"] == "assembled"
    assert detail["swap_compatibility_known"] is True and detail["swap_compatible"] is False
    assert not list(farm["runtime"].staging.iterdir()), "the job removed its staging directory"

    record = (await async_client.get(f"{API}/library/files/{made.id}/assembly")).json()
    assert [part["name"] for part in record["parts"]] == ["frame.stl", "lid.stl"]
    assert record["parts"][1]["quantity"] == 3 and record["layout"]["spacing"] == 2.0
    assert record["printer"]["printer"] == {"source": "local", "id": str(farm["presets"]["x1"])}
    assert record["set_files"] == [{"id": made.id, "filename": made.filename}]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_printer_rows_give_two_files_and_one_may_fail(async_client, db_session, farm):
    body = _body(farm, [("big", {}), ("lid", {})], printers=("x1", "small"))
    job = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    good, bad = job["result"]["printers"]
    assert job["result"]["outcome"] == "ok"
    assert good["status"] == "ok" and len(good["files"]) == 1
    assert bad["status"] == "failed" and bad["reason"] == "part_rejected" and not bad["files"]
    assert [(part["name"], part["reason"]) for part in bad["parts"]] == [("big.stl", "too_large")]
    assert len(await _assembled_files(db_session)) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unresolvable_preset_fails_that_row_by_name(async_client, db_session, farm):
    body = _body(farm, [("lid", {})], printers=("ghost", "x1"))
    job = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    ghost, good = job["result"]["printers"]
    assert ghost["status"] == "failed" and ghost["reason"] == "preset_unresolved"
    assert "Test ghost" in ghost["detail"] and "A parent nobody has" in ghost["detail"]
    assert good["status"] == "ok" and len(await _assembled_files(db_session)) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_no_usable_printer_row_ends_the_job_with_the_reasons(async_client, db_session, farm):
    body = _body(farm, [("lid", {})], printers=("ghost",))
    job = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    assert job["status"] == "completed"
    assert job["result"]["outcome"] == "failed" and job["result"]["reason"] == "no_printer"
    assert job["result"]["printers"][0]["reason"] == "preset_unresolved"
    assert not await _assembled_files(db_session)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_split_plates_and_fill(async_client, db_session, farm):
    body = _body(farm, [("frame", {"quantity": 2}), ("big", {"fill": True})], split_plates=True)
    job = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    printer = job["result"]["printers"][0]
    assert printer["status"] == "ok", printer
    assert [entry["plate"] for entry in printer["files"]] == [0, 1]
    assert printer["fill_counts"] == {"1": 1}, "one 200 mm part fills a 256 mm plate"
    assert [plate["fill"] for plate in printer["plates"]] == [False, True]
    made = await _assembled_files(db_session)
    assert len(made) == 2 and all("plate" in row.filename for row in made)
    assert len({row.file_metadata["assembly_set"] for row in made}) == 1


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_missing_or_trashed_source_is_one_404(async_client, db_session, farm):
    body = _body(farm, [("lid", {})])
    body["parts"].append({"file_id": 999_999})
    assert (await async_client.post(f"{API}/library/assemble", json=body)).status_code == 404
    row = await db_session.get(LibraryFile, farm["files"]["frame"])
    row.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db_session.commit()
    response = await async_client.post(f"{API}/library/assemble", json=_body(farm, [("lid", {}), ("frame", {})]))
    assert response.status_code == 404 and not slice_dispatch._tasks


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_source_that_is_no_model_is_refused_with_its_name(async_client, farm):
    other = await async_client.post(f"{API}/library/assemble", json=_body(farm, [("project", {})]))
    assert other.status_code == 400 and "project.3mf" in other.json()["detail"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_the_request_limits(async_client, farm):
    many = _body(farm, [("lid", {"quantity": 1500}), ("frame", {"quantity": 600})])
    assert (await async_client.post(f"{API}/library/assemble", json=many)).status_code == 400
    filled = _body(farm, [("lid", {"quantity": 1500}), ("frame", {"quantity": 600, "fill": True})])
    filled["targets"] = []
    assert (await async_client.post(f"{API}/library/assemble", json=filled)).status_code == 422
    nine = _body(farm, [("lid", {})], printers=("x1",) * 9)
    assert (await async_client.post(f"{API}/library/assemble", json=nine)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_source_changed_during_the_job_publishes_nothing(async_client, db_session, farm, monkeypatch):
    real = assembly_job.run_assembly

    async def packs_then_the_source_changes(root, **kwargs):
        result = await real(root, **kwargs)
        row = await db_session.get(LibraryFile, farm["files"]["lid"])
        row.file_hash = "f" * 64
        await db_session.commit()
        return result

    monkeypatch.setattr(assembly_job, "run_assembly", packs_then_the_source_changes)
    body = _body(farm, [("lid", {})], printers=("x1", "small"))
    job = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    assert job["status"] == "completed"
    assert job["result"]["outcome"] == "failed" and job["result"]["reason"] == "source_changed"
    assert not await _assembled_files(db_session)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_relayout_with_wider_spacing_writes_a_new_file_and_trashes_the_old(async_client, db_session, farm):
    first = await _finish(
        async_client, await async_client.post(f"{API}/library/assemble", json=_body(farm, [("frame", {}), ("lid", {})]))
    )
    old_id = first["result"]["printers"][0]["files"][0]["library_file_id"]
    body = {
        "parts": [{"quantity": 2}, {"quantity": 5}],
        "targets": [_row(farm)],
        "layout": {"spacing": 6.0},
        "name": "jar",
        "trash_previous": True,
    }
    job = await _finish(async_client, await async_client.post(f"{API}/library/files/{old_id}/relayout", json=body))
    result = job["result"]
    assert result["outcome"] == "ok" and result["trashed"] == [old_id] and result["kept"] == []
    assert result["printers"][0]["plates"][0]["counts"] == {"0": 2, "1": 5}
    (new,) = await _assembled_files(db_session)
    assert new.id != old_id
    record = (await async_client.get(f"{API}/library/files/{new.id}/assembly")).json()
    assert record["layout"]["spacing"] == 6.0 and [p["quantity"] for p in record["parts"]] == [2, 5]
    assert [p["name"] for p in record["parts"]] == ["frame.stl", "lid.stl"]
    assert (await db_session.get(LibraryFile, old_id, populate_existing=True)).deleted_at is not None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_relayout_can_keep_the_previous_file(async_client, db_session, farm):
    first = await _finish(
        async_client, await async_client.post(f"{API}/library/assemble", json=_body(farm, [("lid", {})]))
    )
    old_id = first["result"]["printers"][0]["files"][0]["library_file_id"]
    body = {"parts": [{"quantity": 4}], "targets": [_row(farm)], "name": "jar", "trash_previous": False}
    job = await _finish(async_client, await async_client.post(f"{API}/library/files/{old_id}/relayout", json=body))
    assert job["result"]["outcome"] == "ok" and job["result"]["trashed"] == []
    names = [row.filename for row in await _assembled_files(db_session)]
    assert len(names) == 2 and len(set(names)) == 2, "the second file got a free name"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_relayout_of_a_hand_made_file_is_refused(async_client, db_session, farm):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("Metadata/platenest.json", "{}")
        archive.writestr("Metadata/bamdude_assembly.json", '{"version": 1, "set_id": "x"}')
    path = farm["tmp"] / "library" / "files" / "fake.3mf"
    path.write_bytes(buffer.getvalue())
    plain = LibraryFile(
        filename="plain.3mf", file_path=str(path.relative_to(farm["tmp"])), file_type="3mf", file_size=1
    )
    lying = LibraryFile(
        filename="lying.3mf",
        file_path=str(path.relative_to(farm["tmp"])),
        file_type="3mf",
        file_size=1,
        file_metadata={"assembled": True, "assembly_set": "a" * 32},
    )
    db_session.add_all([plain, lying])
    await db_session.commit()
    body = {"parts": [{"quantity": 1}], "targets": [_row(farm)], "name": "x"}
    for row in (plain, lying):
        for response in (
            await async_client.post(f"{API}/library/files/{row.id}/relayout", json=body),
            await async_client.get(f"{API}/library/files/{row.id}/assembly"),
        ):
            assert response.status_code == 409 and response.json()["detail"]["error"] == "not_assembled"
    assert not slice_dispatch._tasks


@pytest.mark.asyncio
@pytest.mark.integration
async def test_relayout_of_an_incomplete_set_is_refused(async_client, db_session, farm):
    body = _body(farm, [("frame", {}), ("big", {"fill": True})], split_plates=True)
    first = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    one, two = (entry["library_file_id"] for entry in first["result"]["printers"][0]["files"])
    row = await db_session.get(LibraryFile, two)
    row.deleted_at = datetime.now(timezone.utc).replace(tzinfo=None)
    await db_session.commit()
    again = {"parts": [{"quantity": 1}, {"quantity": 1}], "targets": [_row(farm)], "name": "jar"}
    response = await async_client.post(f"{API}/library/files/{one}/relayout", json=again)
    assert response.status_code == 409 and response.json()["detail"]["error"] == "assembly_set_incomplete"
    wrong_parts = {"parts": [{"quantity": 1}], "targets": [_row(farm)], "name": "jar"}
    row.deleted_at = None
    await db_session.commit()
    assert (await async_client.post(f"{API}/library/files/{one}/relayout", json=wrong_parts)).status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cancel_stops_the_job_and_leaves_no_file(async_client, db_session, farm):
    body = _body(farm, [("lid", {"fill": True})], rotation_step=45, spacing=0)
    response = await async_client.post(f"{API}/library/assemble", json=body)
    job_id = response.json()["job_id"]
    for _ in range(400):
        progress = (slice_dispatch.get(job_id).progress or {}).get("stage")
        if progress == "packing":
            break
        await asyncio.sleep(0.05)
    assert progress == "packing"
    assert (await async_client.delete(f"{API}/slice-jobs/{job_id}")).status_code == 204
    job = await _finish(async_client, response)
    assert job["status"] == "failed" and job["error_status"] == 499
    assert not await _assembled_files(db_session)
    for _ in range(200):  # the job is marked stopped at once; its child's end is proven a moment later
        if farm["runtime"].quiesced() and not list(farm["runtime"].staging.iterdir()):
            break
        await asyncio.sleep(0.05)
    assert farm["runtime"].quiesced() and not list(farm["runtime"].staging.iterdir())
    assert (await async_client.delete(f"{API}/slice-jobs/{job_id}")).status_code == 409
    assert (await async_client.delete(f"{API}/slice-jobs/999999")).status_code == 404


@pytest.mark.asyncio
@pytest.mark.integration
async def test_printer_info_reports_the_reach_of_each_nozzle(async_client, farm):
    def ask(key):
        return async_client.get(
            f"{API}/library/assemble/printer-info", params={"source": "local", "id": str(farm["presets"][key])}
        )

    dual = (await ask("dual")).json()
    assert dual["model"] == "H2D" and dual["bed_size"] == [350.0, 350.0]
    assert [(n["index"], n["area"], n["max_height"]) for n in dual["nozzles"]] == [
        (1, [0.0, 0.0, 325.0, 320.0], 320.0),
        (2, [25.0, 0.0, 350.0, 320.0], 325.0),
    ]
    assert len((await ask("x1")).json()["nozzles"]) == 1
    ghost = await ask("ghost")
    assert ghost.status_code == 422 and ghost.json()["detail"]["error"] == "preset_unresolved"
    assert (await ask("process")).status_code == 400, "a preset of another kind is the resolver's own refusal"
    assert (
        await async_client.get(f"{API}/library/assemble/printer-info", params={"source": "nowhere", "id": "1"})
    ).status_code == 422


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_nozzle_the_printer_does_not_have_fails_that_row(async_client, db_session, farm):
    body = _body(farm, [("lid", {})])
    body["targets"] = [_row(farm, "x1", nozzle=2), _row(farm, "dual", nozzle=2)]
    job = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    single, dual = job["result"]["printers"]
    assert single["status"] == "failed" and single["reason"] == "nozzle_unknown"
    assert dual["status"] == "ok"
    (made,) = await _assembled_files(db_session)
    with zipfile.ZipFile(farm["tmp"] / made.file_path) as archive:
        settings = json.loads(archive.read("Metadata/project_settings.config"))
    assert settings["filament_map"] == ["2"] and settings["filament_map_mode"] == "Manual"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_an_unauthenticated_request_never_reaches_the_handler(async_client, farm):
    anonymous = {"Authorization": ""}
    body = _body(farm, [("lid", {})])
    assert (await async_client.post(f"{API}/library/assemble", json=body, headers=anonymous)).status_code == 401
    assert (await async_client.delete(f"{API}/slice-jobs/1", headers=anonymous)).status_code == 401
    assert not slice_dispatch._tasks


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_row_that_fails_between_two_files_takes_the_first_one_back(async_client, db_session, farm, monkeypatch):
    """A printer gets all its files or none: half a "file per plate" set could never be laid out again."""
    from fastapi import HTTPException

    from backend.app.api.routes import library as library_routes

    real, calls = library_routes.save_3mf_bytes_to_library, []

    async def second_file_is_refused(db, **kwargs):
        calls.append(kwargs["filename"])
        if len(calls) == 2:
            raise HTTPException(409, {"code": "destination_changed"})
        return await real(db, **kwargs)

    monkeypatch.setattr(library_routes, "save_3mf_bytes_to_library", second_file_is_refused)
    body = _body(farm, [("frame", {}), ("big", {"fill": True})], split_plates=True)
    job = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    row = job["result"]["printers"][0]
    assert len(calls) == 2 and "plate 1" in calls[0]
    assert (row["status"], row["reason"], row["files"]) == ("failed", "destination_changed", [])
    assert job["result"]["outcome"] == "failed"
    assert not await _assembled_files(db_session)
    leftovers = (await db_session.scalars(select(LibraryFile).where(LibraryFile.source_type == "assembled"))).all()
    assert not leftovers, "taken back for good, not left in the trash"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_job_that_is_saving_its_files_can_no_longer_be_stopped(async_client, farm):
    started = asyncio.Event()

    async def run(job_id):
        slice_dispatch.set_progress(job_id, {"stage": "publishing"})
        started.set()
        await asyncio.sleep(30)
        return {}

    job = await slice_dispatch.enqueue(kind="assembly", source_id=1, source_name="x", run=run)
    await started.wait()
    response = await async_client.delete(f"{API}/slice-jobs/{job.id}")
    assert response.status_code == 409 and job.status == "running"
    slice_dispatch.set_progress(job.id, {"stage": "packing"})
    assert (await async_client.delete(f"{API}/slice-jobs/{job.id}")).status_code == 204


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_source_whose_bytes_changed_on_disk_is_caught_before_packing(async_client, db_session, farm):
    row = await db_session.get(LibraryFile, farm["files"]["lid"])
    (farm["tmp"] / row.file_path).write_bytes(_stl(SHAPES["frame"]))  # the share changed; no scan has seen it yet
    job = await _finish(
        async_client, await async_client.post(f"{API}/library/assemble", json=_body(farm, [("lid", {})]))
    )
    assert job["result"]["outcome"] == "failed" and job["result"]["reason"] == "source_changed"
    assert not await _assembled_files(db_session)


# --- STEP sources (stage C) ---


@pytest.mark.asyncio
@pytest.mark.integration
async def test_without_the_cad_bundle_a_step_source_is_refused_and_meshes_still_assemble(
    async_client, db_session, farm, monkeypatch
):
    from backend.app.api.routes import library_assembly

    monkeypatch.setattr(library_assembly, "_cad_installed", lambda: False)
    refused = await async_client.post(f"{API}/library/assemble", json=_body(farm, [("lid", {}), ("drawing", {})]))
    assert refused.status_code == 409 and "STEP" in refused.json()["detail"]
    done = await _finish(
        async_client, await async_client.post(f"{API}/library/assemble", json=_body(farm, [("lid", {})]))
    )
    assert done["result"]["outcome"] == "ok"


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_step_source_that_cannot_be_converted_fails_the_job_naming_the_part(
    async_client, db_session, farm, monkeypatch
):
    """The fixture's "drawing.step" is no STEP at all (or this checkout has no bundle): either way the job ends
    with that part named and nothing saved — never a layout without it."""
    from backend.app.api.routes import library_assembly

    monkeypatch.setattr(library_assembly, "_cad_installed", lambda: True)
    response = await async_client.post(f"{API}/library/assemble", json=_body(farm, [("lid", {}), ("drawing", {})]))
    result = (await _finish(async_client, response))["result"]
    assert result["outcome"] == "failed" and result["reason"] in ("step_failed", "step_unavailable")
    assert result["parts"] == [{"index": 1, "name": "drawing.step", "reason": result["reason"]}]
    assert await _assembled_files(db_session) == []


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_step_file_larger_than_the_converter_takes_is_refused_by_name(
    async_client, db_session, farm, monkeypatch
):
    from sqlalchemy import update

    from backend.app.api.routes import library_assembly

    monkeypatch.setattr(library_assembly, "_cad_installed", lambda: True)
    await db_session.execute(
        update(LibraryFile).where(LibraryFile.filename == "drawing.step").values(file_size=64 * 1024**2 + 1)
    )
    await db_session.commit()
    refused = await async_client.post(f"{API}/library/assemble", json=_body(farm, [("drawing", {})]))
    assert refused.status_code == 413 and "drawing.step" in refused.json()["detail"]


# --- the deferred minors of the first review ---


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_printer_with_its_origin_inside_the_bed_is_refused_by_name(async_client, db_session, farm):
    ref = _row(farm, "centred")["printer_preset"]
    info = await async_client.get(f"{API}/library/assemble/printer-info", params=ref)
    assert info.status_code == 422 and info.json()["detail"]["error"] == "origin_unsupported"
    body = _body(farm, [("lid", {})], printers=("centred", "x1"))
    result = (await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body)))["result"]
    assert [row["reason"] for row in result["printers"]] == ["origin_unsupported", ""]


async def _relayout_with_trash(async_client, farm, old_id):
    body = {"parts": [{"quantity": 1}, {"quantity": 1}], "targets": [_row(farm)], "name": "jar", "trash_previous": True}
    return (await _finish(async_client, await async_client.post(f"{API}/library/files/{old_id}/relayout", json=body)))[
        "result"
    ]


async def _first(async_client, farm):
    body = _body(farm, [("frame", {}), ("lid", {})])
    first = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    return first["result"]["printers"][0]["files"][0]["library_file_id"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_delete_right_lost_while_the_job_ran_keeps_the_previous_file(
    async_client, db_session, farm, monkeypatch
):
    old_id = await _first(async_client, farm)

    async def lost(db, authority, proof):
        return False, None

    monkeypatch.setattr(assembly_job, "_delete_rights", lost)
    result = await _relayout_with_trash(async_client, farm, old_id)
    assert result["outcome"] == "ok" and result["trashed"] == [] and result["kept"] == [old_id]
    assert (await db_session.get(LibraryFile, old_id, populate_existing=True)).deleted_at is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_previous_file_on_an_external_share_is_reported_as_removed_not_trashed(async_client, db_session, farm):
    """An external file has no trash: its row goes and its bytes stay on the share — the result says so."""
    old_id = await _first(async_client, farm)
    row = await db_session.get(LibraryFile, old_id)
    row.is_external = True
    await db_session.commit()
    result = await _relayout_with_trash(async_client, farm, old_id)
    assert result["trashed"] == [] and result["removed"] == [old_id] and result["kept"] == []
    assert await db_session.get(LibraryFile, old_id, populate_existing=True) is None


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_copies_of_one_plate_are_not_a_whole_set(async_client, db_session, farm):
    body = _body(farm, [("frame", {}), ("big", {"fill": True})], split_plates=True)
    first = await _finish(async_client, await async_client.post(f"{API}/library/assemble", json=body))
    one, two = (entry["library_file_id"] for entry in first["result"]["printers"][0]["files"])
    first_row, second_row = await db_session.get(LibraryFile, one), await db_session.get(LibraryFile, two)
    second_row.file_hash = first_row.file_hash  # the second file is a copy of the first
    await db_session.commit()
    again = {"parts": [{"quantity": 1}, {"quantity": 1}], "targets": [_row(farm)], "name": "jar"}
    response = await async_client.post(f"{API}/library/files/{one}/relayout", json=again)
    assert response.status_code == 409 and response.json()["detail"]["error"] == "assembly_set_incomplete"
