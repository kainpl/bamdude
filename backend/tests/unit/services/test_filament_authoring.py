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
