"""Strict flattening of one preset for "Assemble project": a parent that cannot be found is an error."""

import json
from datetime import datetime, timezone

import pytest

from backend.app.models.local_preset import LocalPreset
from backend.app.models.orca_base_cache import OrcaBaseProfile
from backend.app.schemas.slicer import PresetRef
from backend.app.services import orca_profiles
from backend.app.services.assembly.presets import PresetUnresolved, flatten
from backend.app.services.orca_profiles import MissingBaseProfile, resolve_preset


async def _seed(db, name: str, profile_type: str, content: dict) -> None:
    db.add(
        OrcaBaseProfile(
            name=name,
            profile_type=profile_type,
            setting=json.dumps({"name": name, **content}),
            fetched_at=datetime.now(timezone.utc),
        )
    )
    await db.flush()


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    class _Refuses:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url):
            raise orca_profiles.httpx.ConnectError("no network in tests")

    monkeypatch.setattr(orca_profiles.httpx, "AsyncClient", _Refuses)


@pytest.mark.asyncio
async def test_standard_stub_flattens_through_the_chain(db_session):
    await _seed(db_session, "fdm_machine_common", "machine", {"printable_height": "250", "nozzle_diameter": ["0.6"]})
    await _seed(db_session, "fdm_x_common", "machine", {"inherits": "fdm_machine_common", "printer_model": "X"})
    await _seed(db_session, "P1", "machine", {"inherits": "fdm_x_common", "nozzle_diameter": ["0.4"]})
    p = await flatten(db_session, None, PresetRef(source="standard", id="P1"), "printer", credential_snapshots=[])
    assert (p.slot, p.name, p.parent, p.changed) == ("printer", "P1", "", ())
    assert (
        p.flat["printable_height"] == "250" and p.flat["nozzle_diameter"] == ["0.4"] and p.flat["printer_model"] == "X"
    )


@pytest.mark.asyncio
async def test_user_delta_reports_its_parent_and_changed_keys(db_session):
    await _seed(db_session, "fdm_process_common", "process", {"layer_height": "0.2", "wall_loops": "2", "seam": "a"})
    await _seed(db_session, "0.20mm Standard @X", "process", {"inherits": "fdm_process_common", "seam": "b"})
    row = LocalPreset(
        name="My fine",
        preset_type="process",
        source="orcaslicer",
        inherits="0.20mm Standard @X",
        setting=json.dumps(
            {
                "name": "My fine",
                "inherits": "0.20mm Standard @X",
                "layer_height": "0.12",
                "wall_loops": "4",
                "seam": "b",
            }
        ),
    )
    db_session.add(row)
    await db_session.flush()
    p = await flatten(db_session, None, PresetRef(source="local", id=str(row.id)), "process", credential_snapshots=[])
    assert p.name == "My fine" and p.parent == "0.20mm Standard @X" and p.changed == ("layer_height", "wall_loops")
    assert p.flat["seam"] == "b" and p.flat["layer_height"] == "0.12"


@pytest.mark.asyncio
async def test_a_root_user_preset_has_no_parent_and_no_changed_keys(db_session):
    row = LocalPreset(
        name="Root", preset_type="filament", source="manual", setting=json.dumps({"name": "Root", "a": "1"})
    )
    db_session.add(row)
    await db_session.flush()
    p = await flatten(db_session, None, PresetRef(source="local", id=str(row.id)), "filament", credential_snapshots=[])
    assert (p.parent, p.changed, p.flat["a"]) == ("", (), "1")


@pytest.mark.asyncio
async def test_missing_parent_is_an_error_naming_it(db_session):
    with pytest.raises(PresetUnresolved) as error:
        await flatten(db_session, None, PresetRef(source="standard", id="Nope"), "filament", credential_snapshots=[])
    assert (error.value.slot, error.value.name, error.value.missing) == ("filament", "Nope", "Nope")


@pytest.mark.asyncio
async def test_a_broken_link_in_the_middle_names_the_missing_parent(db_session):
    await _seed(db_session, "Child", "filament", {"inherits": "Gone"})
    with pytest.raises(PresetUnresolved) as error:
        await flatten(db_session, None, PresetRef(source="standard", id="Child"), "filament", credential_snapshots=[])
    assert error.value.missing == "Gone"


@pytest.mark.asyncio
async def test_resolve_preset_default_is_still_lenient(db_session):
    data = {"name": "x", "inherits": "Nope"}
    assert await resolve_preset(dict(data), "filament", db_session) == data
    with pytest.raises(MissingBaseProfile) as error:
        await resolve_preset(dict(data), "filament", db_session, strict=True)
    assert error.value.name == "Nope"


@pytest.mark.asyncio
async def test_an_expired_cached_parent_still_serves_a_farm_without_internet(db_session):
    """The cache's age says when to look for a newer copy, not when to forget the one we have."""
    from datetime import timedelta

    old = datetime.now(timezone.utc) - timedelta(days=orca_profiles.CACHE_TTL_DAYS + 30)
    for name, content in (
        ("fdm_machine_common", {"printable_height": "250"}),
        ("P1", {"inherits": "fdm_machine_common"}),
    ):
        db_session.add(
            OrcaBaseProfile(
                name=name, profile_type="machine", setting=json.dumps({"name": name, **content}), fetched_at=old
            )
        )
    await db_session.flush()
    preset = await flatten(db_session, None, PresetRef(source="standard", id="P1"), "printer", credential_snapshots=[])
    assert preset.flat["printable_height"] == "250"
    with pytest.raises(PresetUnresolved):
        await flatten(
            db_session, None, PresetRef(source="standard", id="Never seen"), "printer", credential_snapshots=[]
        )
