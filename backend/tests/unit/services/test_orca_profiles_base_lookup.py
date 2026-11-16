"""A base profile is found by its NAME through the vendor manifest, not by guessing its file.

``fetch_and_cache_base_profile`` asked GitHub for ``BBL/<category>/<name>.json``.
Two kinds of preset are not there (measured on OrcaSlicer 2.5.0):

- presets a brand keeps in a subfolder — ``filament/P1P/``, ``filament/Polymaker/``,
  ``filament/SUNLU/``, 150 files;
- presets whose name holds a "/" — ``Bambu Support For PLA/PETG @BBL A1`` is the
  file ``Bambu Support For PLA-PETG @BBL A1.json``, 47 names.

So a user's preset made from one of those lost its parent: imported half-empty,
and refused by "Assemble project", which embeds presets strictly. The slicer
itself looks a name up in the vendor manifest (``BBL.json``: name -> sub_path);
so do we now. Same rule as the slicer sidecar's index (G12).
"""

from __future__ import annotations

import urllib.parse

import pytest

from backend.app.services import orca_profiles

BASE = orca_profiles.ORCA_BASE_URL
MANIFEST_URL = f"{BASE}.json"


class _GitHub:
    """What raw.githubusercontent.com would answer, by decoded URL."""

    def __init__(self, files: dict[str, dict], manifest: dict | None):
        self.files = {f"{BASE}/{path}": body for path, body in files.items()}
        if manifest is not None:
            self.files[MANIFEST_URL] = manifest
        self.asked: list[str] = []

    def client(self):
        github = self

        class _Response:
            def __init__(self, body):
                self.status_code = 200 if body is not None else 404
                self._body = body

            def json(self):
                return self._body

        class _Client:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url):
                decoded = urllib.parse.unquote(str(url))
                github.asked.append(decoded)
                return _Response(github.files.get(decoded))

        return _Client


def _manifest(**lists: list[tuple[str, str]]) -> dict:
    return {key: [{"name": name, "sub_path": sub_path} for name, sub_path in entries] for key, entries in lists.items()}


P1P = "Bambu ABS @BBL P1P 0.2 nozzle"
SLASH = "Bambu Support For PLA/PETG @BBL A1"
TOP = "Bambu PLA Basic @BBL X1C"

FILES = {
    "filament/fdm_filament_common.json": {"name": "fdm_filament_common", "filament_flow_ratio": ["1"]},
    "filament/fdm_filament_abs.json": {
        "name": "fdm_filament_abs",
        "inherits": "fdm_filament_common",
        "filament_type": ["ABS"],
    },
    f"filament/P1P/{P1P}.json": {"name": P1P, "inherits": "fdm_filament_abs", "nozzle_temperature": ["270"]},
    "filament/Bambu Support For PLA-PETG @BBL A1.json": {"name": SLASH, "nozzle_temperature": ["210"]},
    f"filament/{TOP}.json": {"name": TOP, "nozzle_temperature": ["220"]},
}

MANIFEST = _manifest(
    filament_list=[
        ("fdm_filament_common", "filament/fdm_filament_common.json"),
        ("fdm_filament_abs", "filament/fdm_filament_abs.json"),
        (P1P, f"filament/P1P/{P1P}.json"),
        (SLASH, "filament/Bambu Support For PLA-PETG @BBL A1.json"),
        (TOP, f"filament/{TOP}.json"),
    ],
    machine_list=[],
    process_list=[],
)


@pytest.fixture
def github(monkeypatch):
    orca_profiles.forget_vendor_manifest()

    def install(files=FILES, manifest=MANIFEST) -> _GitHub:
        fake = _GitHub(files, manifest)
        monkeypatch.setattr(orca_profiles.httpx, "AsyncClient", fake.client())
        return fake

    yield install
    orca_profiles.forget_vendor_manifest()


async def test_a_parent_in_a_brand_subfolder_is_found(db_session, github):
    github()
    base = await orca_profiles.fetch_and_cache_base_profile(P1P, "filament", db_session)
    assert base is not None
    assert base["nozzle_temperature"] == ["270"]


async def test_a_parent_with_a_slash_in_its_name_is_found(db_session, github):
    github()
    base = await orca_profiles.fetch_and_cache_base_profile(SLASH, "filament", db_session)
    assert base is not None
    assert base["nozzle_temperature"] == ["210"]


async def test_a_preset_at_the_top_of_its_category_is_still_found(db_session, github):
    github()
    base = await orca_profiles.fetch_and_cache_base_profile(TOP, "filament", db_session)
    assert base == FILES[f"filament/{TOP}.json"]


async def test_a_user_preset_made_from_a_subfolder_preset_flattens_whole(db_session, github):
    # What "Assemble project" asks: strict, because it must not embed half a preset.
    github()
    flat = await orca_profiles.resolve_preset(
        {"name": "My ABS", "inherits": P1P, "fan_max_speed": ["30"]}, "filament", db_session, strict=True
    )
    assert flat["nozzle_temperature"] == ["270"]
    assert flat["filament_type"] == ["ABS"]
    assert flat["filament_flow_ratio"] == ["1"]
    assert flat["fan_max_speed"] == ["30"]


async def test_a_name_is_never_turned_into_a_folder(db_session, github):
    fake = github()
    await orca_profiles.fetch_and_cache_base_profile(SLASH, "filament", db_session)
    assert f"{BASE}/filament/{SLASH}.json" not in fake.asked


async def test_a_name_the_manifest_gives_two_files_is_not_resolved(db_session, github):
    files = {
        "filament/Twin.json": {"name": "Twin", "nozzle_temperature": ["200"]},
        "filament/Other/Twin copy.json": {"name": "Twin", "nozzle_temperature": ["240"]},
    }
    fake = github(
        files,
        _manifest(filament_list=[("Twin", "filament/Twin.json"), ("Twin", "filament/Other/Twin copy.json")]),
    )
    assert await orca_profiles.fetch_and_cache_base_profile("Twin", "filament", db_session) is None
    # Neither file was fetched: there is no "the first one".
    assert [url for url in fake.asked if url != MANIFEST_URL] == []


async def test_the_same_file_listed_twice_is_one_file(db_session, github):
    github(
        {"filament/Solo.json": {"name": "Solo", "nozzle_temperature": ["205"]}},
        _manifest(filament_list=[("Solo", "filament/Solo.json"), ("Solo", "filament/Solo.json")]),
    )
    base = await orca_profiles.fetch_and_cache_base_profile("Solo", "filament", db_session)
    assert base is not None


async def test_a_printer_base_is_looked_up_in_the_machine_list(db_session, github):
    github(
        {"machine/fdm_machine_common.json": {"name": "fdm_machine_common", "gcode_flavor": "marlin"}},
        _manifest(filament_list=[], machine_list=[("fdm_machine_common", "machine/fdm_machine_common.json")]),
    )
    base = await orca_profiles.fetch_and_cache_base_profile("fdm_machine_common", "printer", db_session)
    assert base == {"name": "fdm_machine_common", "gcode_flavor": "marlin"}


async def test_without_the_manifest_the_plain_address_still_works(db_session, github):
    # GitHub answered the file but not the manifest: no worse than before.
    fake = github(manifest=None)
    base = await orca_profiles.fetch_and_cache_base_profile(TOP, "filament", db_session)
    assert base is not None
    assert f"{BASE}/filament/{TOP}.json" in fake.asked


async def test_without_the_manifest_a_file_of_another_name_is_not_taken(db_session, github):
    github({"filament/Alias.json": {"name": "Somebody Else"}}, manifest=None)
    assert await orca_profiles.fetch_and_cache_base_profile("Alias", "filament", db_session) is None


async def test_the_manifest_is_fetched_once_for_a_whole_chain(db_session, github):
    fake = github()
    await orca_profiles.resolve_preset({"name": "My ABS", "inherits": P1P}, "filament", db_session, strict=True)
    assert fake.asked.count(MANIFEST_URL) == 1


async def test_a_manifest_that_could_not_be_fetched_is_not_asked_for_again_at_once(db_session, github):
    """Without internet every lookup used to pay the manifest's timeout again
    before the plain-address attempts."""
    fake = github(manifest=None)

    await orca_profiles.fetch_and_cache_base_profile(TOP, "filament", db_session)
    await orca_profiles.fetch_and_cache_base_profile("fdm_filament_abs", "filament", db_session)

    assert fake.asked.count(MANIFEST_URL) == 1
    # The plain address is still tried each time.
    assert f"{BASE}/filament/fdm_filament_abs.json" in fake.asked


async def test_a_failed_manifest_is_asked_for_again_after_a_while(db_session, github, monkeypatch):
    fake = github(manifest=None)
    await orca_profiles.fetch_and_cache_base_profile(TOP, "filament", db_session)

    monkeypatch.setattr(orca_profiles, "MANIFEST_RETRY_AFTER", orca_profiles.timedelta(0))
    fake.files[MANIFEST_URL] = MANIFEST
    base = await orca_profiles.fetch_and_cache_base_profile(P1P, "filament", db_session)

    assert fake.asked.count(MANIFEST_URL) == 2
    assert base is not None and base["nozzle_temperature"] == ["270"]
