"""Orca push of authored families: client-minted profile uuids, the server
updated_time as the optimistic-lock anchor, conflicts detected BEFORE the
network push, and the two explicit resolutions (overwrite cloud / adopt
cloud). Mirrors the Bambu-leg doctrine: explicit re-push only, best-effort
per preset, the family stays canonical in BamDude."""

import json
import uuid as uuid_mod
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.models.local_preset import LocalPreset
from backend.app.models.user_filament import UserFilamentFamily, UserFilamentPreset
from backend.app.services.filament_authoring import AuthoringError
from backend.app.services.filament_push import PUSH_CAPABLE, push_blobs, push_family, resolve_push_conflict
from backend.app.services.orca_cloud import OrcaCloudConflict


def _seed_root(db_session, *, fid="Pabc1234", name="Poly PETG B @Bambu Lab P1S 0.4 nozzle"):
    preset = LocalPreset(
        name=name,
        preset_type="filament",
        source="authored",
        setting=json.dumps({"name": name, "filament_id": fid, "filament_type": ["PETG"]}),
    )
    db_session.add(preset)
    db_session.add(
        UserFilamentFamily(
            filament_id=fid,
            ecosystem="local",
            alias="Poly PETG B",
            vendor="Poly",
            filament_type="PETG",
            origin="authored",
        )
    )
    return preset


async def _mirror_for(db_session, preset, fid, **cols):
    await db_session.flush()
    row = UserFilamentPreset(
        owner_user_id=None,
        ecosystem="orca",
        source="local",
        local_preset_id=preset.id,
        name=preset.name,
        family_filament_id=fid,
        **cols,
    )
    db_session.add(row)
    await db_session.commit()
    return row


def _orca_mock(profiles=None):
    svc = AsyncMock()
    svc.is_authenticated = True
    svc.list_profiles.return_value = profiles or []
    svc.push_profile.return_value = {"id": "whatever", "updated_time": 1800000100}
    svc.force_push_profile.return_value = {"id": "whatever", "updated_time": 1800000200}
    return svc


def _patched(svc):
    return patch("backend.app.services.filament_push._build_orca_cloud", new=AsyncMock(return_value=svc))


UUID1 = "11111111-1111-1111-1111-111111111111"


class TestOrcaPushFamily:
    def test_capability_is_on(self):
        assert PUSH_CAPABLE["orca"] is True

    @pytest.mark.asyncio
    async def test_create_mints_a_uuid_and_writes_the_anchor(self, db_session):
        preset = _seed_root(db_session)
        row = await _mirror_for(db_session, preset, "Pabc1234")
        svc = _orca_mock()

        with _patched(svc):
            results = await push_family(db_session, filament_id="Pabc1234", ecosystem="orca")

        assert [r["status"] for r in results] == ["pushed"]
        await db_session.refresh(row)
        uuid_mod.UUID(row.orca_pushed_profile_id)  # a real uuid was minted
        assert row.orca_pushed_updated_time == 1800000100
        assert row.orca_push_dirty is False
        assert row.orca_pushed_at is not None
        kwargs = svc.push_profile.call_args.kwargs
        assert kwargs["content"]["filament_id"] == "Pabc1234"
        assert "original_updated_time" not in kwargs

    @pytest.mark.asyncio
    async def test_update_carries_the_anchor_as_the_lock(self, db_session):
        preset = _seed_root(db_session)
        row = await _mirror_for(
            db_session,
            preset,
            "Pabc1234",
            orca_pushed_profile_id=UUID1,
            orca_pushed_updated_time=1800000001,
            orca_push_dirty=True,
        )
        svc = _orca_mock(profiles=[{"id": UUID1, "updated_time": 1800000001}])

        with _patched(svc):
            results = await push_family(db_session, filament_id="Pabc1234", ecosystem="orca")

        assert [r["status"] for r in results] == ["updated"]
        assert svc.push_profile.call_args.kwargs["original_updated_time"] == 1800000001
        await db_session.refresh(row)
        assert row.orca_pushed_updated_time == 1800000100
        assert row.orca_push_dirty is False

    @pytest.mark.asyncio
    async def test_a_moved_cloud_copy_is_a_conflict_without_a_network_push(self, db_session):
        preset = _seed_root(db_session)
        row = await _mirror_for(
            db_session,
            preset,
            "Pabc1234",
            orca_pushed_profile_id=UUID1,
            orca_pushed_updated_time=1800000001,
            orca_push_dirty=True,
        )
        svc = _orca_mock(profiles=[{"id": UUID1, "updated_time": 1800000099}])

        with _patched(svc):
            results = await push_family(db_session, filament_id="Pabc1234", ecosystem="orca")

        assert results[0]["status"] == "conflict"
        assert results[0]["server_updated_time"] == 1800000099
        assert results[0]["row_id"] == row.id
        svc.push_profile.assert_not_called()
        await db_session.refresh(row)
        assert row.orca_push_dirty is True  # nothing was resolved

    @pytest.mark.asyncio
    async def test_a_vanished_cloud_copy_is_recreated_under_a_fresh_id(self, db_session):
        preset = _seed_root(db_session)
        row = await _mirror_for(
            db_session,
            preset,
            "Pabc1234",
            orca_pushed_profile_id=UUID1,
            orca_pushed_updated_time=1800000001,
            orca_push_dirty=True,
        )
        svc = _orca_mock(profiles=[])  # deleted in the cloud

        with _patched(svc):
            results = await push_family(db_session, filament_id="Pabc1234", ecosystem="orca")

        assert [r["status"] for r in results] == ["pushed"]
        await db_session.refresh(row)
        assert row.orca_pushed_profile_id != UUID1

    @pytest.mark.asyncio
    async def test_a_tombstone_id_is_retried_once_with_a_new_uuid(self, db_session):
        preset = _seed_root(db_session)
        await _mirror_for(db_session, preset, "Pabc1234")
        svc = _orca_mock()
        svc.push_profile.side_effect = [
            OrcaCloudConflict(code=-3, reason="tombstone_uuid_conflict", server_profile=None),
            {"id": "fresh", "updated_time": 1800000100},
        ]

        with _patched(svc):
            results = await push_family(db_session, filament_id="Pabc1234", ecosystem="orca")

        assert [r["status"] for r in results] == ["pushed"]
        first, second = svc.push_profile.call_args_list
        assert first.kwargs["profile_id"] != second.kwargs["profile_id"]

    @pytest.mark.asyncio
    async def test_clean_pushed_row_is_up_to_date(self, db_session):
        preset = _seed_root(db_session)
        await _mirror_for(
            db_session,
            preset,
            "Pabc1234",
            orca_pushed_profile_id=UUID1,
            orca_pushed_updated_time=1800000001,
            orca_push_dirty=False,
        )
        svc = _orca_mock(profiles=[{"id": UUID1, "updated_time": 1800000001}])

        with _patched(svc):
            results = await push_family(db_session, filament_id="Pabc1234", ecosystem="orca")

        assert [r["status"] for r in results] == ["up_to_date"]
        svc.push_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_not_connected_raises(self, db_session):
        preset = _seed_root(db_session)
        await _mirror_for(db_session, preset, "Pabc1234")

        with (
            patch("backend.app.services.filament_push._build_orca_cloud", new=AsyncMock(return_value=None)),
            pytest.raises(AuthoringError, match="not connected"),
        ):
            await push_family(db_session, filament_id="Pabc1234", ecosystem="orca")


class TestOrcaPushBlobs:
    @pytest.mark.asyncio
    async def test_cloud_only_creation_without_bookkeeping(self, db_session):
        svc = _orca_mock()

        with _patched(svc):
            results = await push_blobs(
                db_session, blobs=[{"name": "Poly PETG B @X1C", "filament_type": ["PETG"]}], ecosystem="orca"
            )

        assert [r["status"] for r in results] == ["pushed"]
        kwargs = svc.push_profile.call_args.kwargs
        uuid_mod.UUID(kwargs["profile_id"])
        assert kwargs["name"] == "Poly PETG B @X1C"


class TestResolvePushConflict:
    async def _conflicted(self, db_session):
        preset = _seed_root(db_session)
        row = await _mirror_for(
            db_session,
            preset,
            "Pabc1234",
            orca_pushed_profile_id=UUID1,
            orca_pushed_updated_time=1800000001,
            orca_push_dirty=True,
            pushed_cloud_id="PFUS_OLD",
        )
        return preset, row

    @pytest.mark.asyncio
    async def test_force_overwrites_the_cloud_copy(self, db_session):
        preset, row = await self._conflicted(db_session)
        svc = _orca_mock()

        with _patched(svc):
            out = await resolve_push_conflict(db_session, row_id=row.id, action="force")

        assert out["status"] == "overwritten"
        kwargs = svc.force_push_profile.call_args.kwargs
        assert kwargs["profile_id"] == UUID1
        assert kwargs["content"]["filament_id"] == "Pabc1234"
        await db_session.refresh(row)
        assert row.orca_pushed_updated_time == 1800000200
        assert row.orca_push_dirty is False

    @pytest.mark.asyncio
    async def test_adopt_takes_the_cloud_content_and_marks_bambu_stale(self, db_session):
        preset, row = await self._conflicted(db_session)
        cloud_content = {"name": "edited in slicer", "filament_type": ["PETG"], "filament_id": "Pabc1234"}
        svc = _orca_mock(profiles=[{"id": UUID1, "updated_time": 1800000099, "content": cloud_content}])

        with _patched(svc):
            out = await resolve_push_conflict(db_session, row_id=row.id, action="adopt")

        assert out["status"] == "adopted"
        await db_session.refresh(preset)
        assert json.loads(preset.setting) == cloud_content
        await db_session.refresh(row)
        assert row.orca_push_dirty is False
        assert row.orca_pushed_updated_time == 1800000099
        assert row.push_dirty is True  # the Bambu copy now lags — say so honestly
        svc.force_push_profile.assert_not_called()

    @pytest.mark.asyncio
    async def test_adopt_of_a_vanished_profile_fails_cleanly(self, db_session):
        preset, row = await self._conflicted(db_session)
        svc = _orca_mock(profiles=[])

        with _patched(svc), pytest.raises(AuthoringError, match="gone"):
            await resolve_push_conflict(db_session, row_id=row.id, action="adopt")

    @pytest.mark.asyncio
    async def test_unknown_row_fails_cleanly(self, db_session):
        svc = _orca_mock()
        with _patched(svc), pytest.raises(AuthoringError):
            await resolve_push_conflict(db_session, row_id=999999, action="force")


class TestOrcaDeleteLeg:
    @pytest.mark.asyncio
    async def test_family_delete_with_also_cloud_batches_the_ids(self, db_session):
        from backend.app.services.filament_authoring import _delete_orca_pushed_copies

        preset = _seed_root(db_session)
        row = await _mirror_for(db_session, preset, "Pabc1234", orca_pushed_profile_id=UUID1)
        svc = _orca_mock()
        svc.delete_profiles.return_value = {"status": "ok", "deleted": [{"id": UUID1, "name": row.name}]}

        with _patched(svc):
            deleted = await _delete_orca_pushed_copies(db_session, [row], None)

        assert deleted == 1
        svc.delete_profiles.assert_awaited_once_with([UUID1])

    @pytest.mark.asyncio
    async def test_partial_failure_is_tolerated(self, db_session):
        from backend.app.services.filament_authoring import _delete_orca_pushed_copies

        preset = _seed_root(db_session)
        row = await _mirror_for(db_session, preset, "Pabc1234", orca_pushed_profile_id=UUID1)
        svc = _orca_mock()
        svc.delete_profiles.return_value = {"status": "partial_failure", "deleted": [], "failed": [{"id": UUID1}]}

        with _patched(svc):
            deleted = await _delete_orca_pushed_copies(db_session, [row], None)

        assert deleted == 0

    @pytest.mark.asyncio
    async def test_nothing_pushed_makes_no_network_call(self, db_session):
        from backend.app.services.filament_authoring import _delete_orca_pushed_copies

        preset = _seed_root(db_session)
        row = await _mirror_for(db_session, preset, "Pabc1234")
        svc = _orca_mock()

        with _patched(svc):
            deleted = await _delete_orca_pushed_copies(db_session, [row], None)

        assert deleted == 0
        svc.delete_profiles.assert_not_called()
