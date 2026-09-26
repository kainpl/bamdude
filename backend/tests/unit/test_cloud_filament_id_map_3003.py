"""A custom preset's own filament_id is read from either place Bambu Cloud puts
it (upstream 9434875f, #3003).

The detail endpoint returns a preset's own ``filament_id`` either on the
response envelope or inside the preset JSON under ``setting`` — BambuStudio
writes it into the JSON of a family ROOT only. ``/cloud/filament-id-map``
read only the envelope, so a custom filament whose id sits under ``setting``
was missing from the map and its K-profiles (keyed by that id) were shown as
a raw ``P…`` code on the Profiles page and in the print dialog.

BamDude's slot assignment does not have this defect: it takes the id from the
listing, which the cloud serves already resolved (vault
``60-specs/bs-filament-preset-system``, measured on a live account).
"""

from __future__ import annotations

import pytest

from backend.app.api.routes import cloud as cloud_routes


class _FakeCloud:
    is_authenticated = True

    def __init__(self, details: dict[str, dict]):
        self._details = details
        self.closed = False

    async def get_slicer_settings(self) -> dict:
        return {"filament": {"private": [{"setting_id": sid} for sid in self._details]}}

    async def get_setting_detail(self, setting_id: str) -> dict:
        return self._details[setting_id]

    async def close(self) -> None:
        self.closed = True


@pytest.fixture
def fake_cloud(monkeypatch):
    def install(details: dict[str, dict]) -> _FakeCloud:
        fake = _FakeCloud(details)

        async def _build(db, user):
            return fake

        monkeypatch.setattr(cloud_routes, "build_authenticated_cloud", _build)
        monkeypatch.setattr(cloud_routes, "_filament_id_name_cache", {})
        monkeypatch.setattr(cloud_routes, "_filament_id_name_cache_time", 0)
        return fake

    return install


@pytest.mark.asyncio
async def test_an_id_inside_setting_is_found(fake_cloud):
    fake = fake_cloud(
        {
            "PFUS9ddc938fe3ab8f": {
                "name": "Devil Design PLA Basic @Bambu Lab A1 0.4 nozzle",
                "setting": {"filament_id": "P4d64437", "filament_type": "PLA"},
            }
        }
    )
    result = await cloud_routes.get_filament_id_map(db=None, current_user=None)
    assert result == {"P4d64437": "Devil Design PLA Basic"}
    assert fake.closed


@pytest.mark.asyncio
async def test_the_envelope_still_wins(fake_cloud):
    fake_cloud(
        {
            "PFUSaaaa": {
                "name": "Sunlu PETG @Bambu Lab X1 Carbon 0.4 nozzle",
                "filament_id": "P1234567",
                "setting": {"filament_id": "P7654321"},
            }
        }
    )
    result = await cloud_routes.get_filament_id_map(db=None, current_user=None)
    assert result == {"P1234567": "Sunlu PETG"}


@pytest.mark.asyncio
async def test_a_preset_with_no_id_anywhere_is_left_out(fake_cloud):
    fake_cloud({"PFUSbbbb": {"name": "Orphan @Bambu Lab A1 0.4 nozzle", "setting": "not a dict"}})
    result = await cloud_routes.get_filament_id_map(db=None, current_user=None)
    assert result == {}


# The other half of #3003: configure_ams_slot sent a cloud setting id as
# tray_info_idx, which the printer truncates to 8 characters and acknowledges
# (measured upstream: PFUS9ddc938fe3ab8f read back as PFUS9DDC) — the slot then
# resolves to nothing. Here the slot is built by build_slot_assignment from a
# family id; anything that is not a family never reaches the field.
@pytest.mark.parametrize(
    "not_a_family",
    ["PFUS9ddc938fe3ab8f", "PFCN0123456789abcd", "3f2b8c1e-5a4d-4c2b-9e1f-0a1b2c3d4e5f", "PLA"],
)
@pytest.mark.asyncio
async def test_a_cloud_id_or_material_name_never_reaches_tray_info_idx(db_session, not_a_family):
    from backend.app.services.slot_assignment import build_slot_assignment

    plan = await build_slot_assignment(
        db_session,
        family_id=not_a_family,
        printer_model="A1 Mini",
        nozzle_diameter="0.4",
        material_override="PLA",
        supports_user_preset=True,
    )
    assert plan.tray_info_idx == "GFL99"
    assert plan.tray_type == "PLA"
