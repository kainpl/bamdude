"""Authoring core (spec B §1): BS CreatePresetsDialog parity for name
validation and get_filament_id's logged-out P-hash."""

import hashlib

import pytest
from sqlalchemy import select

from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services.filament_authoring import (
    FILAMENT_TYPES,
    AuthoringError,
    build_family_name,
    mint_filament_id,
    strip_special_keys,
    validate_vendor,
)


def test_type_list_mirrors_bs_and_is_unique():
    assert "PETG" in FILAMENT_TYPES and "Misc" in FILAMENT_TYPES
    assert len(FILAMENT_TYPES) == len(set(FILAMENT_TYPES))  # BS's literal dupe PETG removed


def test_strip_special_keys_is_bs_set():
    assert strip_special_keys("Po@ly;\tMaker\n") == "PolyMaker"


@pytest.mark.parametrize("vendor", ["Bambu", "bambu", "Generic", "GENERIC", "12345", "", "@;"])
def test_vendor_refusals(vendor):
    with pytest.raises(AuthoringError):
        validate_vendor(vendor)


def test_family_name_shape():
    assert build_family_name("Poly", "PETG", "Basic") == "Poly PETG Basic"
    with pytest.raises(AuthoringError):
        build_family_name("Poly", "NOTATYPE", "Basic")
    with pytest.raises(AuthoringError):
        build_family_name("Poly", "PETG", "   ")


@pytest.mark.asyncio
async def test_mint_is_deterministic_md5(db_session):
    fid, attached = await mint_filament_id(db_session, "Poly PETG Basic")
    assert attached is False
    assert fid == "P" + hashlib.md5(b"Poly PETG Basic").hexdigest()[:7]
    fid2, _ = await mint_filament_id(db_session, "Poly PETG Basic")
    assert fid2 == fid  # same name -> same id, every time


@pytest.mark.asyncio
async def test_mint_adopts_existing_id_on_name_match(db_session):
    """BS convergence: a known pre-'@' name returns ITS id (attached=True)."""
    db_session.add(
        UserFilamentPreset(
            owner_user_id=None,
            ecosystem="bambu",
            source="cloud_bambu",
            cloud_id="PFUS_X",
            name="Poly PETG Basic @Bambu Lab P1S",
            family_filament_id="Pdeadbee",
        )
    )
    await db_session.commit()
    fid, attached = await mint_filament_id(db_session, "Poly PETG Basic")
    assert (fid, attached) == ("Pdeadbee", True)


@pytest.mark.asyncio
async def test_mint_rehashes_on_collision_with_different_name(db_session):
    natural = "P" + hashlib.md5(b"Poly PETG Basic").hexdigest()[:7]
    db_session.add(
        UserFilamentFamily(
            filament_id=natural,
            ecosystem="local",
            alias="Some Other Name",
            vendor=None,
            filament_type=None,
            origin="authored",
        )
    )
    await db_session.commit()
    fid, attached = await mint_filament_id(db_session, "Poly PETG Basic")
    assert attached is False
    assert fid != natural
    assert len(fid) == 8 and fid.startswith("P")


# `select` is used by the Task-4 tests appended below.
_ = select


# ---------------------------------------------------------------------------
# Content clones + lifecycle (spec B §2–§3)
# ---------------------------------------------------------------------------

import json  # noqa: E402
from unittest.mock import AsyncMock, patch  # noqa: E402

from backend.app.models.local_preset import LocalPreset  # noqa: E402
from backend.app.models.printer import Printer  # noqa: E402
from backend.app.models.spool import Spool  # noqa: E402
from backend.app.services.filament_authoring import (  # noqa: E402
    FamilyInUseError,
    create_family,
    delete_family,
)

_BASE_CONTENT = {
    "name": "Generic PETG",
    "filament_vendor": ["Generic"],
    "filament_type": ["PETG"],
    "nozzle_temperature_range_low": ["220"],
    "nozzle_temperature_range_high": ["260"],
    "compatible_printers": ["Bambu Lab P1S 0.4 nozzle"],
    "from": "system",
}


def _add_printer(db_session, name="P1S", model="P1S"):
    p = Printer(name=name, model=model, ip_address="10.0.0.9", serial_number="S" + name, access_code="x")
    db_session.add(p)
    return p


@pytest.mark.asyncio
async def test_create_family_from_type_clones_roots(db_session):
    printer = _add_printer(db_session)
    await db_session.commit()

    with patch(
        "backend.app.services.filament_authoring._resolve_bundled_content",
        new=AsyncMock(return_value=dict(_BASE_CONTENT)),
    ):
        result = await create_family(
            db_session,
            vendor="Poly",
            filament_type="PETG",
            serial="Basic",
            printer_ids=[printer.id],
            source_mode="type",
        )
    await db_session.commit()

    assert result.attached is False and result.filament_id.startswith("P")
    (root,) = result.roots
    assert root.error is None and root.printer_name == "Bambu Lab P1S 0.4 nozzle"

    preset = (await db_session.execute(select(LocalPreset))).scalars().one()
    blob = json.loads(preset.setting)
    assert preset.source == "authored"
    assert blob["name"] == "Poly PETG Basic @Bambu Lab P1S 0.4 nozzle"
    assert blob["filament_id"] == result.filament_id
    assert blob["filament_vendor"] == ["Poly"] and blob["filament_type"] == ["PETG"]
    assert blob["compatible_printers"] == ["Bambu Lab P1S 0.4 nozzle"]
    assert "inherits" not in blob and "setting_id" not in blob

    fam = (await db_session.execute(select(UserFilamentFamily))).scalars().one()
    assert (fam.ecosystem, fam.origin, fam.alias) == ("local", "authored", "Poly PETG Basic")
    # absorbed: exactly one mirror row, family-linked
    mirror = (await db_session.execute(select(UserFilamentPreset))).scalars().one()
    assert mirror.family_filament_id == result.filament_id and mirror.source == "local"


@pytest.mark.asyncio
async def test_create_family_identity_only_without_sidecar(db_session):
    printer = _add_printer(db_session)
    await db_session.commit()
    with patch(
        "backend.app.services.filament_authoring._resolve_bundled_content",
        new=AsyncMock(return_value=None),
    ):
        result = await create_family(
            db_session,
            vendor="Poly",
            filament_type="PETG",
            serial="NoSidecar",
            printer_ids=[printer.id],
            source_mode="type",
        )
    await db_session.commit()
    (root,) = result.roots
    assert root.local_preset_id is None and root.error
    assert result.warnings  # visible notice per spec §2
    assert (await db_session.execute(select(UserFilamentFamily))).scalars().one()  # identity exists


@pytest.mark.asyncio
async def test_delete_family_refused_while_referenced(db_session):
    db_session.add(
        UserFilamentFamily(
            filament_id="Pfeed001",
            ecosystem="local",
            alias="Poly PETG Del",
            vendor="Poly",
            filament_type="PETG",
            origin="authored",
        )
    )
    db_session.add(Spool(brand="B", material="PETG", filament_family_id="Pfeed001"))
    await db_session.commit()
    with pytest.raises(FamilyInUseError) as e:
        await delete_family(db_session, filament_id="Pfeed001")
    assert e.value.spools == 1


@pytest.mark.asyncio
async def test_delete_family_removes_roots_and_mirrors(db_session):
    printer = _add_printer(db_session, name="X1C", model="X1C")
    await db_session.commit()
    with patch(
        "backend.app.services.filament_authoring._resolve_bundled_content",
        new=AsyncMock(return_value=dict(_BASE_CONTENT)),
    ):
        result = await create_family(
            db_session,
            vendor="Poly",
            filament_type="PETG",
            serial="Gone",
            printer_ids=[printer.id],
        )
    await db_session.commit()
    summary = await delete_family(db_session, filament_id=result.filament_id)
    await db_session.commit()
    assert summary["presets_deleted"] == 1
    assert not (await db_session.execute(select(LocalPreset))).scalars().all()
    assert not (await db_session.execute(select(UserFilamentPreset))).scalars().all()
    assert not (await db_session.execute(select(UserFilamentFamily))).scalars().all()


@pytest.mark.asyncio
async def test_create_family_by_printer_names(db_session):
    """BS-style targeting: the caller picks printer PROFILES (preset names),
    no BamDude device involved."""
    with patch(
        "backend.app.services.filament_authoring._resolve_bundled_content",
        new=AsyncMock(return_value=dict(_BASE_CONTENT)),
    ):
        result = await create_family(
            db_session,
            vendor="Poly",
            filament_type="PETG",
            serial="ByName",
            printer_ids=[],
            printer_names=["Bambu Lab P1S 0.4 nozzle"],
        )
    await db_session.commit()
    (root,) = result.roots
    assert root.printer_id is None and root.printer_name == "Bambu Lab P1S 0.4 nozzle"
    preset = (await db_session.execute(select(LocalPreset))).scalars().one()
    blob = json.loads(preset.setting)
    assert blob["compatible_printers"] == ["Bambu Lab P1S 0.4 nozzle"]
    assert blob["name"] == "Poly PETG ByName @Bambu Lab P1S 0.4 nozzle"


@pytest.mark.asyncio
async def test_create_family_cloud_only_returns_blobs_without_local_rows(db_session):
    """save_local=False (Bambu-tab flow): identity + blobs for the push,
    but no LocalPreset and no mirror row — the sync will mirror the cloud."""
    with patch(
        "backend.app.services.filament_authoring._resolve_bundled_content",
        new=AsyncMock(return_value=dict(_BASE_CONTENT)),
    ):
        result = await create_family(
            db_session,
            vendor="Poly",
            filament_type="PETG",
            serial="CloudOnly",
            printer_ids=[],
            printer_names=["Bambu Lab P1S 0.4 nozzle"],
            save_local=False,
        )
    await db_session.commit()
    (root,) = result.roots
    assert root.error is None and root.local_preset_id is None
    assert len(result.blobs) == 1
    assert result.blobs[0]["filament_id"] == result.filament_id
    assert not (await db_session.execute(select(LocalPreset))).scalars().all()
    assert not (await db_session.execute(select(UserFilamentPreset))).scalars().all()
    # identity still exists — slots and K work immediately
    assert (await db_session.execute(select(UserFilamentFamily))).scalars().one()


def test_catalog_lists_bs_printer_profile_names():
    from backend.app.utils import filament_catalog as catalog

    names = catalog.all_printer_names("bambu")
    assert "Bambu Lab P1S 0.4 nozzle" in names
    assert "Bambu Lab X1 Carbon 0.4 nozzle" in names
    assert names == sorted(set(names))
