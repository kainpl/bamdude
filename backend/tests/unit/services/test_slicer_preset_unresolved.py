"""A preset the slicer sidecar cannot resolve refuses the slice (G12, owner's decision 4).

The sidecar used to drop an ``inherits`` it could not follow and slice on the
CLI's defaults; it now answers ``422 {"error": "preset_unresolved", ...}``.
These tests pin our half: the client turns that answer into a refusal that
names the preset and the missing parent, the route keeps it out of the
embedded-settings retry, the settings panel reads it as a reason, and the
sentence reaches the Slice dialog in the system language.
"""

from __future__ import annotations

import httpx
import pytest

from backend.app.i18n import api_errors, set_language_cache
from backend.app.services.slicer_api import (
    SlicerApiService,
    SlicerInputError,
    SlicerPresetUnresolvedError,
)


def _service(status: int, body: dict) -> SlicerApiService:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code=status, json=body)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), timeout=10.0)
    return SlicerApiService("http://sidecar:3000", client=client)


def _unresolved(preset: str | None, parent: str, category: str = "filament") -> dict:
    return {
        "message": f'Preset "{preset}" cannot be resolved: its parent "{parent}" is not among this slicer\'s profiles',
        "details": "Nothing was sliced.",
        "error": "preset_unresolved",
        "category": category,
        "preset": preset,
        "missing_parent": parent,
    }


async def _slice(service: SlicerApiService):
    return await service.slice_with_profiles(
        model_bytes=b"x",
        model_filename="Cube.stl",
        printer_profile_json="{}",
        process_profile_json="{}",
        filament_profile_jsons=["{}"],
    )


@pytest.fixture
def ukrainian():
    api_errors.reload_catalog()
    set_language_cache("uk")
    yield
    set_language_cache("en")


class TestSliceRefusal:
    @pytest.mark.asyncio
    async def test_a_missing_parent_is_a_refusal_that_names_preset_and_parent(self):
        service = _service(422, _unresolved("My PETG", "Bambu PETG Basic @BBL H9"))
        with pytest.raises(SlicerPresetUnresolvedError) as exc_info:
            await _slice(service)
        exc = exc_info.value
        assert (exc.preset, exc.missing_parent, exc.category) == ("My PETG", "Bambu PETG Basic @BBL H9", "filament")
        assert "'My PETG'" in str(exc)
        assert "'Bambu PETG Basic @BBL H9'" in str(exc)
        assert "nothing was sliced" in str(exc)

    @pytest.mark.asyncio
    async def test_it_is_an_input_error_so_the_route_never_retries_with_embedded_settings(self):
        # library._slice... retries with the file's own settings only on
        # SlicerApiServerError; a refusal that reached that retry would be
        # sliced on settings the operator did not pick.
        service = _service(422, _unresolved("My PETG", "Gone"))
        with pytest.raises(SlicerInputError):
            await _slice(service)

    @pytest.mark.asyncio
    async def test_a_stock_preset_the_slicer_does_not_have_is_named_once(self):
        name = "Bambu PETG HF @BBL H3C"
        service = _service(422, _unresolved(name, name))
        with pytest.raises(SlicerPresetUnresolvedError) as exc_info:
            await _slice(service)
        text = str(exc_info.value)
        assert text.count(name) == 1
        assert "parent" not in text

    @pytest.mark.asyncio
    async def test_a_profile_without_a_name_is_reported_by_its_parent(self):
        service = _service(422, _unresolved(None, "Gone"))
        with pytest.raises(SlicerPresetUnresolvedError) as exc_info:
            await _slice(service)
        assert "'Gone'" in str(exc_info.value)
        assert "None" not in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_the_profile_less_path_reads_the_same_answer(self):
        service = _service(422, _unresolved("My PETG", "Gone"))
        with pytest.raises(SlicerPresetUnresolvedError):
            await service.slice_without_profiles(model_bytes=b"x", model_filename="Cube.3mf")

    @pytest.mark.asyncio
    async def test_another_422_stays_a_plain_input_error(self):
        service = _service(422, {"message": "Something else"})
        with pytest.raises(SlicerInputError) as exc_info:
            await _slice(service)
        assert not isinstance(exc_info.value, SlicerPresetUnresolvedError)
        assert "Something else" in str(exc_info.value)


class TestSettingsPanelReason:
    @pytest.mark.asyncio
    async def test_resolve_profile_reports_the_preset_not_the_sidecar(self):
        # The panel has a sentence for each reason; "sidecar unavailable" for a
        # preset the sidecar answered about would send the operator to Docker.
        service = _service(422, _unresolved("My Process", "0.20mm Standard @BBL H9", "process"))
        resolved = await service.resolve_profile('{"name": "My Process", "inherits": "x"}', "process")
        assert resolved.values is None
        assert resolved.reason == "preset_unresolved"

    @pytest.mark.asyncio
    async def test_any_other_failure_is_still_the_sidecars(self):
        service = _service(500, {"message": "boom"})
        resolved = await service.resolve_profile("{}", "process")
        assert resolved.reason == "sidecar_unavailable"


class TestTheSentenceReachesTheDialogTranslated:
    @pytest.mark.asyncio
    async def test_both_sentences_have_ukrainian(self, ukrainian):
        for preset, parent in (("My PETG", "Bambu PETG Basic @BBL H9"), ("Stock @BBL H3C", "Stock @BBL H3C")):
            with pytest.raises(SlicerPresetUnresolvedError) as exc_info:
                await _slice(_service(422, _unresolved(preset, parent)))
            english = str(exc_info.value)
            translated = api_errors.translate_sentence(english, "uk")
            assert translated != english
            assert parent in translated
            assert preset in translated

    @pytest.mark.asyncio
    async def test_a_failed_slice_job_answers_in_the_system_language(self, ukrainian, monkeypatch):
        # The Slice dialog shows a failed job's ``error_detail``. The job stores
        # the English sentence (it outlives the request that raised it), so the
        # polling route is the boundary that translates it.
        from backend.app.api.routes import slice_jobs
        from backend.app.services.slice_dispatch import SliceJob

        with pytest.raises(SlicerPresetUnresolvedError) as exc_info:
            await _slice(_service(422, _unresolved("My PETG", "Gone")))
        job = SliceJob(id=7, kind="library_file", source_id=1, source_name="Cube.3mf")
        job.status = "failed"
        job.error_status = 400
        job.error_detail = str(exc_info.value)

        class _Jobs:
            def get(self, job_id):
                return job

        monkeypatch.setattr(slice_jobs, "slice_dispatch", _Jobs())
        body = await slice_jobs.get_slice_job(7, auth=(None, True))

        assert body["error_status"] == 400
        assert body["error_detail"] != job.error_detail
        assert "My PETG" in body["error_detail"]
        assert "не" in body["error_detail"]

    @pytest.mark.asyncio
    async def test_an_english_system_gets_the_sentence_as_raised(self, monkeypatch):
        from backend.app.api.routes import slice_jobs
        from backend.app.services.slice_dispatch import SliceJob

        set_language_cache("en")
        job = SliceJob(id=8, kind="library_file", source_id=1, source_name="Cube.3mf")
        job.status = "failed"
        job.error_status = 400
        job.error_detail = "Slicer rejected input (400): nope"

        class _Jobs:
            def get(self, job_id):
                return job

        monkeypatch.setattr(slice_jobs, "slice_dispatch", _Jobs())
        body = await slice_jobs.get_slice_job(8, auth=(None, True))
        assert body["error_detail"] == "Slicer rejected input (400): nope"
