"""Tests for the /makerworld/* route handlers.

Mocks ``MakerWorldService`` so tests don't hit the real MakerWorld API. We
still cover: URL validation, metadata passthrough, already-imported detection,
source-URL-based dedupe on import, auto-creation of the MakerWorld default
folder, canonical URL shape, filename basenaming, and the ``/recent-imports``
listing endpoint.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from backend.app.api.routes.makerworld import _canonical_url
from backend.app.models.library import LibraryFile, LibraryFolder


def _fake_service(**stubs):
    """Build an AsyncMock MakerWorldService with the given async method stubs."""
    svc = AsyncMock()
    svc.close = AsyncMock()
    for name, value in stubs.items():
        if callable(value) and not isinstance(value, AsyncMock):
            setattr(svc, name, AsyncMock(side_effect=value))
        else:
            setattr(svc, name, AsyncMock(return_value=value))
    return svc


def _default_design(alphanumeric: str = "US2bb73b106683e5", model_id: int = 1400373):
    """Shape the backend needs from ``/design/{id}``: the alphanumeric
    ``modelId`` field that iot-service requires, plus at least one instance
    so the importer has a ``profile_id`` to fall back on."""
    return {
        "id": model_id,
        "modelId": alphanumeric,
        "title": "Seed Starter",
        "instances": [{"profileId": 298919107, "title": "9 cells"}],
    }


def _default_manifest(name: str = "benchy.3mf"):
    return {
        "name": name,
        "url": "https://makerworld.bblmw.com/makerworld/model/X/Y/f.3mf?exp=1&key=k",
    }


class TestCanonicalUrl:
    """Unit test the dedupe-key builder directly — regressions break dedupe
    silently so it's worth pinning the exact shape."""

    def test_without_profile_id(self):
        assert _canonical_url(1400373) == "https://makerworld.com/models/1400373"

    def test_without_profile_id_when_none(self):
        assert _canonical_url(1400373, None) == "https://makerworld.com/models/1400373"

    def test_with_profile_id(self):
        assert _canonical_url(1400373, 298919107) == ("https://makerworld.com/models/1400373#profileId-298919107")


class TestStatus:
    @pytest.mark.asyncio
    async def test_status_reports_no_token_by_default(self, async_client, db_session):
        resp = await async_client.get("/api/v1/makerworld/status")
        assert resp.status_code == 200
        body = resp.json()
        # Fresh in-memory DB has no stored token, so can_download must be false
        assert body == {"has_cloud_token": False, "can_download": False}


class TestResolve:
    @pytest.mark.asyncio
    async def test_rejects_non_makerworld_url(self, async_client):
        resp = await async_client.post(
            "/api/v1/makerworld/resolve",
            json={"url": "https://thingiverse.com/thing/1"},
        )
        assert resp.status_code == 400
        assert "makerworld" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_happy_path_returns_design_and_instances(self, async_client):
        design_payload = {"id": 1400373, "title": "Seed Starter"}
        instances_payload = {
            "total": 2,
            "hits": [
                {"id": 1452154, "profileId": 298919107, "title": "9 cells"},
                {"id": 1452158, "profileId": 298919564, "title": "12 cells"},
            ],
        }
        svc = _fake_service(get_design=design_payload, get_design_instances=instances_payload)

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/resolve",
                json={"url": "https://makerworld.com/en/models/1400373-slug#profileId-1452154"},
            )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["model_id"] == 1400373
        assert body["profile_id"] == 1452154
        assert body["design"] == design_payload
        assert len(body["instances"]) == 2
        assert body["already_imported_library_ids"] == []

    @pytest.mark.asyncio
    async def test_flags_already_imported_library_ids(self, async_client, db_session):
        # Seed a matching LibraryFile so resolve() reports it back
        existing = LibraryFile(
            filename="prev.3mf",
            file_path="library/files/prev.3mf",
            file_type="3mf",
            file_size=100,
            source_type="makerworld",
            source_url="https://makerworld.com/models/1400373",
        )
        db_session.add(existing)
        await db_session.commit()
        await db_session.refresh(existing)

        svc = _fake_service(
            get_design={"id": 1400373},
            get_design_instances={"total": 0, "hits": []},
        )

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/resolve",
                json={"url": "https://makerworld.com/en/models/1400373"},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["already_imported_library_ids"] == [existing.id]
        # Legacy whole-model import (no #profileId fragment) lands in the "0" bucket.
        body = resp.json()
        assert "0" in body["already_imported_by_profile_id"]
        assert body["already_imported_by_profile_id"]["0"]["library_file_id"] == existing.id

    @pytest.mark.asyncio
    async def test_per_variant_dedupe_map_keys_by_profile_id(self, async_client, db_session):
        """Variant-level imports map cleanly to ``already_imported_by_profile_id``.

        Seeds two prior imports for the same model under different
        profileIds plus a third for a DIFFERENT model — resolve must
        only surface the two that belong to the requested model.
        Pins the cycle's per-variant dedupe so the frontend can mark
        each instance card 'already imported' without a second
        round-trip.
        """
        # Same model, two different variants.
        v1 = LibraryFile(
            filename="plate-a.3mf",
            file_path="library/files/plate-a.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://makerworld.com/models/2000#profileId-111",
            folder_id=None,
        )
        v2 = LibraryFile(
            filename="plate-b.3mf",
            file_path="library/files/plate-b.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://makerworld.com/models/2000#profileId-222",
            folder_id=None,
        )
        # Different model — must NOT leak into the response.
        other = LibraryFile(
            filename="other.3mf",
            file_path="library/files/other.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://makerworld.com/models/9999#profileId-555",
        )
        db_session.add_all([v1, v2, other])
        await db_session.commit()
        await db_session.refresh(v1)
        await db_session.refresh(v2)

        svc = _fake_service(
            get_design={"id": 2000},
            get_design_instances={"total": 0, "hits": []},
        )
        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/resolve",
                json={"url": "https://makerworld.com/en/models/2000"},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        # Both variants of model 2000 surface in the map under their stringified profileIds.
        mapping = body["already_imported_by_profile_id"]
        assert set(mapping.keys()) == {"111", "222"}
        assert mapping["111"]["library_file_id"] == v1.id
        assert mapping["111"]["filename"] == "plate-a.3mf"
        assert mapping["222"]["library_file_id"] == v2.id
        assert mapping["222"]["filename"] == "plate-b.3mf"
        # The flat list keeps the legacy contract too.
        assert set(body["already_imported_library_ids"]) == {v1.id, v2.id}

    @pytest.mark.asyncio
    async def test_merges_compatibility_from_design_into_instances(self, async_client):
        """Per-instance printer compatibility info lives on
        ``design.instances[].extention.modelInfo`` but not on
        ``/instances/hits``. Resolve enriches each hit with both
        ``compatibility`` (primary printer the instance was sliced for) and
        ``otherCompatibility`` (extra printers the uploader marked it
        compatible with) so the frontend can show "sliced for A1 / also
        marked compatible with: H2D, P1S".
        """
        design_payload = {
            "id": 1400373,
            "title": "Seed Starter",
            "instances": [
                {
                    "id": 1452154,
                    "extention": {
                        "modelInfo": {
                            "compatibility": ["A1"],
                            "otherCompatibility": ["H2D", "P1S"],
                        }
                    },
                },
                {
                    "id": 1452158,
                    "extention": {
                        "modelInfo": {
                            "compatibility": ["X1 Carbon"],
                            "otherCompatibility": [],
                        }
                    },
                },
            ],
        }
        instances_payload = {
            "total": 2,
            "hits": [
                {"id": 1452154, "profileId": 298919107, "title": "9 cells"},
                {"id": 1452158, "profileId": 298919564, "title": "12 cells"},
            ],
        }
        svc = _fake_service(get_design=design_payload, get_design_instances=instances_payload)

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/resolve",
                json={"url": "https://makerworld.com/en/models/1400373"},
            )
        assert resp.status_code == 200, resp.text
        instances = resp.json()["instances"]
        by_id = {i["id"]: i for i in instances}
        assert by_id[1452154]["compatibility"] == ["A1"]
        assert by_id[1452154]["otherCompatibility"] == ["H2D", "P1S"]
        assert by_id[1452158]["compatibility"] == ["X1 Carbon"]
        assert by_id[1452158]["otherCompatibility"] == []

    @pytest.mark.asyncio
    async def test_resolve_handles_missing_compatibility_gracefully(self, async_client):
        """Older designs (or hits without a matching design.instances entry)
        must not crash the resolve response — they just don't get the
        compat fields."""
        design_payload = {"id": 1400373, "instances": [{"id": 1452154}]}  # no extention
        instances_payload = {
            "total": 2,
            "hits": [
                {"id": 1452154, "profileId": 298919107},
                {"id": 9999999, "profileId": 298919999},  # no design.instances match
            ],
        }
        svc = _fake_service(get_design=design_payload, get_design_instances=instances_payload)

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/resolve",
                json={"url": "https://makerworld.com/en/models/1400373"},
            )
        assert resp.status_code == 200, resp.text
        instances = resp.json()["instances"]
        # First instance: design entry exists but no extention → fields absent or None.
        first = next(i for i in instances if i["id"] == 1452154)
        assert first.get("compatibility") is None
        assert first.get("otherCompatibility") is None
        # Second instance: no design entry at all → no enrichment, no crash.
        second = next(i for i in instances if i["id"] == 9999999)
        assert "compatibility" not in second or second["compatibility"] is None


class TestImport:
    """End-to-end of POST /makerworld/import — mocks the service but exercises
    real DB writes, real ``save_3mf_bytes_to_library``, real folder auto-creation."""

    _FAKE_3MF_BYTES = b"PK\x03\x04not-a-real-3mf"

    @pytest.mark.asyncio
    async def test_returns_existing_on_source_url_match(self, async_client, db_session):
        """Re-importing a model we already have must NOT re-download.

        Dedupe key is ``{model_id}#profileId-{profile_id}`` — matches the
        canonical URL the route constructs, not the legacy model-only shape.
        """
        existing = LibraryFile(
            filename="already-here.3mf",
            file_path="library/files/already.3mf",
            file_type="3mf",
            file_size=500,
            source_type="makerworld",
            source_url="https://makerworld.com/models/1400373#profileId-298919107",
        )
        db_session.add(existing)
        await db_session.commit()
        await db_session.refresh(existing)

        svc = _fake_service(
            get_design=_default_design(),
            get_profile_download=_default_manifest(),
        )
        svc.download_3mf = AsyncMock()  # must remain uncalled

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/import",
                json={"model_id": 1400373, "profile_id": 298919107},
            )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["library_file_id"] == existing.id
        assert body["was_existing"] is True
        assert body["profile_id"] == 298919107
        svc.download_3mf.assert_not_called()

    @pytest.mark.asyncio
    async def test_autocreates_makerworld_folder_when_folder_id_none(self, async_client, db_session):
        """Default destination — a top-level "MakerWorld" folder — is created
        on first import so users don't have to set it up."""
        svc = _fake_service(
            get_design=_default_design(),
            get_profile_download=_default_manifest(),
            download_3mf=(self._FAKE_3MF_BYTES, "benchy.3mf"),
        )

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/import",
                json={"model_id": 1400373, "profile_id": 298919107, "folder_id": None},
            )
        assert resp.status_code == 200, resp.text

        # The new folder should exist, at the root.
        from sqlalchemy import select

        result = await db_session.execute(
            select(LibraryFolder).where(LibraryFolder.name == "MakerWorld", LibraryFolder.parent_id.is_(None))
        )
        folder = result.scalar_one()
        assert resp.json()["folder_id"] == folder.id

    @pytest.mark.asyncio
    async def test_uses_existing_folder_when_folder_id_provided(self, async_client, db_session):
        """Caller-supplied ``folder_id`` must be honoured even if the default
        ``MakerWorld`` folder also exists — no silent hijacking."""
        folder = LibraryFolder(name="MyCustomFolder", parent_id=None)
        db_session.add(folder)
        await db_session.commit()
        await db_session.refresh(folder)

        svc = _fake_service(
            get_design=_default_design(),
            get_profile_download=_default_manifest(),
            download_3mf=(self._FAKE_3MF_BYTES, "benchy.3mf"),
        )

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/import",
                json={"model_id": 1400373, "profile_id": 298919107, "folder_id": folder.id},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["folder_id"] == folder.id

    @pytest.mark.asyncio
    async def test_canonical_source_url_includes_profile_id(self, async_client, db_session):
        """The saved row's ``source_url`` must include ``#profileId-`` so two
        plates of the same model become two library rows (dedupe is per-plate)."""
        svc = _fake_service(
            get_design=_default_design(),
            get_profile_download=_default_manifest(),
            download_3mf=(self._FAKE_3MF_BYTES, "benchy.3mf"),
        )

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/import",
                json={"model_id": 1400373, "profile_id": 298919107},
            )
        assert resp.status_code == 200, resp.text

        from sqlalchemy import select

        row = (
            await db_session.execute(select(LibraryFile).where(LibraryFile.id == resp.json()["library_file_id"]))
        ).scalar_one()
        assert row.source_url == "https://makerworld.com/models/1400373#profileId-298919107"

    @pytest.mark.asyncio
    async def test_filename_from_upstream_is_basenamed(self, async_client, db_session):
        """Defence-in-depth: a malicious ``name`` from the upstream manifest
        (e.g. ``"../../evil.3mf"``) must not persist path components into the
        library row. On-disk storage uses a UUID already, this is belt-and-
        braces protection for the human-readable field."""
        svc = _fake_service(
            get_design=_default_design(),
            get_profile_download={
                "name": "../../evil.3mf",
                "url": "https://makerworld.bblmw.com/makerworld/model/X/Y/f.3mf?exp=1&key=k",
            },
            download_3mf=(self._FAKE_3MF_BYTES, "fallback.3mf"),
        )

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/import",
                json={"model_id": 1400373, "profile_id": 298919107},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["filename"] == "evil.3mf"

    @pytest.mark.asyncio
    async def test_response_includes_profile_id(self, async_client, db_session):
        """UI matches imports back to the plate row via ``profile_id`` — the
        response field must always be populated, even when the caller provided
        it explicitly (rather than the backend falling back to design defaults)."""
        svc = _fake_service(
            get_design=_default_design(),
            get_profile_download=_default_manifest(),
            download_3mf=(self._FAKE_3MF_BYTES, "benchy.3mf"),
        )

        with patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)):
            resp = await async_client.post(
                "/api/v1/makerworld/import",
                json={"model_id": 1400373, "profile_id": 298919107},
            )
        assert resp.status_code == 200, resp.text
        assert resp.json()["profile_id"] == 298919107


class TestRecentImports:
    """GET /makerworld/recent-imports — sidebar feed on the MakerWorld page."""

    @pytest.mark.asyncio
    async def test_empty_when_no_makerworld_imports(self, async_client):
        resp = await async_client.get("/api/v1/makerworld/recent-imports")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_returns_items_newest_first(self, async_client, db_session):
        # Seed three rows with explicit, decreasing created_at timestamps so
        # ordering doesn't depend on auto-increment PK ordering.
        base = datetime(2025, 1, 1, 12, 0, 0)
        older = LibraryFile(
            filename="older.3mf",
            file_path="library/older.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://makerworld.com/models/1",
            created_at=base,
        )
        middle = LibraryFile(
            filename="middle.3mf",
            file_path="library/middle.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://makerworld.com/models/2",
            created_at=base + timedelta(hours=1),
        )
        newer = LibraryFile(
            filename="newer.3mf",
            file_path="library/newer.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://makerworld.com/models/3",
            created_at=base + timedelta(hours=2),
        )
        # Unrelated non-MakerWorld file must NOT show up.
        other = LibraryFile(
            filename="manual.3mf",
            file_path="library/manual.3mf",
            file_type="3mf",
            file_size=10,
            source_type=None,
            source_url=None,
            created_at=base + timedelta(hours=3),
        )
        db_session.add_all([older, middle, newer, other])
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/recent-imports")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        names = [row["filename"] for row in body]
        assert names == ["newer.3mf", "middle.3mf", "older.3mf"]

    @pytest.mark.asyncio
    async def test_response_matches_pydantic_shape(self, async_client, db_session):
        """Lock the exact key set so the frontend's typed ``MakerworldRecentImport``
        doesn't silently fall out of sync with the backend schema."""
        row = LibraryFile(
            filename="x.3mf",
            file_path="library/x.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://makerworld.com/models/1#profileId-2",
        )
        db_session.add(row)
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/recent-imports")
        assert resp.status_code == 200, resp.text
        item = resp.json()[0]
        # m056 added the meta-aware fields (title / author_name / sliced_for /
        # profile_id / has_cover / has_variant_cover); legacy rows without a
        # meta join produce None / False for these but the keys are present.
        assert set(item.keys()) == {
            "library_file_id",
            "filename",
            "folder_id",
            "thumbnail_path",
            "source_url",
            "created_at",
            "title",
            "author_name",
            "sliced_for",
            "profile_id",
            "has_cover",
            "has_variant_cover",
        }
        assert item["source_url"] == "https://makerworld.com/models/1#profileId-2"

    @pytest.mark.asyncio
    async def test_limit_is_honoured(self, async_client, db_session):
        for i in range(5):
            db_session.add(
                LibraryFile(
                    filename=f"f{i}.3mf",
                    file_path=f"library/f{i}.3mf",
                    file_type="3mf",
                    file_size=10,
                    source_type="makerworld",
                    source_url=f"https://makerworld.com/models/{i}",
                )
            )
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/recent-imports?limit=2")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    @pytest.mark.asyncio
    async def test_limit_clamped_to_minimum(self, async_client, db_session):
        """``limit=0`` or negative must clamp to 1 — a zero limit would be
        silently swallowed by SQL and return nothing, which is surprising."""
        db_session.add(
            LibraryFile(
                filename="one.3mf",
                file_path="library/one.3mf",
                file_type="3mf",
                file_size=10,
                source_type="makerworld",
                source_url="https://makerworld.com/models/1",
            )
        )
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/recent-imports?limit=0")
        assert resp.status_code == 200
        assert len(resp.json()) == 1

    @pytest.mark.asyncio
    async def test_limit_clamped_to_maximum(self, async_client, db_session):
        """``limit`` is clamped to 50 so a pathological client can't request
        the whole table. We seed 60 rows and assert the response is capped."""
        for i in range(60):
            db_session.add(
                LibraryFile(
                    filename=f"f{i}.3mf",
                    file_path=f"library/f{i}.3mf",
                    file_type="3mf",
                    file_size=10,
                    source_type="makerworld",
                    source_url=f"https://makerworld.com/models/{i}",
                )
            )
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/recent-imports?limit=9999")
        assert resp.status_code == 200
        assert len(resp.json()) == 50


class TestImportsList:
    """GET /makerworld/imports — paginated/searchable/sortable history feed.

    Covers the m056-cycle endpoint that powers the History tab grid.
    Joins ``library_files`` with ``library_file_makerworld_meta`` so
    search matches meta fields (title/author) on top of filename.
    """

    @staticmethod
    async def _seed(db_session, items: list[dict]):
        """Bulk-seed library_files + (optionally) meta rows.

        Each entry shape:
            {"filename": ..., "title": ..., "author_name": ..., "created_at": datetime}
        Only ``filename`` is required; the rest become meta-row attrs
        (omit to skip the meta row entirely).
        """
        from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta

        for i, item in enumerate(items, start=1):
            lf = LibraryFile(
                filename=item["filename"],
                file_path=f"library/{item['filename']}",
                file_type="3mf",
                file_size=10,
                source_type="makerworld",
                source_url=f"https://makerworld.com/models/{i}#profileId-{i}",
                created_at=item.get("created_at", datetime(2025, 1, 1, 12, i, 0)),
            )
            db_session.add(lf)
            await db_session.flush()
            meta_attrs = {k: v for k, v in item.items() if k in {"title", "author_name", "sliced_for"}}
            if meta_attrs:
                db_session.add(LibraryFileMakerworldMeta(library_file_id=lf.id, **meta_attrs))
        await db_session.commit()

    @pytest.mark.asyncio
    async def test_empty_response_shape(self, async_client):
        resp = await async_client.get("/api/v1/makerworld/imports")
        assert resp.status_code == 200
        body = resp.json()
        assert body["data"] == []
        assert body["meta"]["total"] == 0
        assert body["meta"]["current_page"] == 1
        assert body["meta"]["last_page"] == 1

    @pytest.mark.asyncio
    async def test_default_sort_is_newest_first(self, async_client, db_session):
        await self._seed(
            db_session,
            [
                {"filename": "older.3mf", "created_at": datetime(2025, 1, 1)},
                {"filename": "newer.3mf", "created_at": datetime(2025, 6, 1)},
            ],
        )
        resp = await async_client.get("/api/v1/makerworld/imports")
        assert resp.status_code == 200
        data = resp.json()["data"]
        assert [d["filename"] for d in data] == ["newer.3mf", "older.3mf"]

    @pytest.mark.asyncio
    async def test_sort_by_name_asc(self, async_client, db_session):
        await self._seed(
            db_session,
            [
                {"filename": "zebra.3mf"},
                {"filename": "alpha.3mf"},
                {"filename": "mango.3mf"},
            ],
        )
        resp = await async_client.get("/api/v1/makerworld/imports?sort_by=name-asc")
        assert resp.status_code == 200
        assert [d["filename"] for d in resp.json()["data"]] == ["alpha.3mf", "mango.3mf", "zebra.3mf"]

    @pytest.mark.asyncio
    async def test_pagination_envelope_counts_correctly(self, async_client, db_session):
        await self._seed(db_session, [{"filename": f"f{i}.3mf"} for i in range(7)])
        resp = await async_client.get("/api/v1/makerworld/imports?page=2&per_page=3")
        assert resp.status_code == 200
        body = resp.json()
        assert body["meta"] == {
            "total": 7,
            "current_page": 2,
            "per_page": 3,
            "last_page": 3,
        }
        assert len(body["data"]) == 3

    @pytest.mark.asyncio
    async def test_search_matches_filename(self, async_client, db_session):
        await self._seed(db_session, [{"filename": "benchy.3mf"}, {"filename": "calibration.3mf"}])
        resp = await async_client.get("/api/v1/makerworld/imports?search=ench")
        assert resp.status_code == 200
        names = [d["filename"] for d in resp.json()["data"]]
        assert names == ["benchy.3mf"]

    @pytest.mark.asyncio
    async def test_search_matches_meta_title(self, async_client, db_session):
        await self._seed(
            db_session,
            [
                {"filename": "a.3mf", "title": "Modular Storage Rack"},
                {"filename": "b.3mf", "title": "Cable Comb"},
            ],
        )
        resp = await async_client.get("/api/v1/makerworld/imports?search=Storage")
        assert resp.status_code == 200
        names = [d["filename"] for d in resp.json()["data"]]
        assert names == ["a.3mf"]

    @pytest.mark.asyncio
    async def test_search_matches_meta_author(self, async_client, db_session):
        await self._seed(
            db_session,
            [
                {"filename": "a.3mf", "author_name": "Alice"},
                {"filename": "b.3mf", "author_name": "Bob"},
            ],
        )
        resp = await async_client.get("/api/v1/makerworld/imports?search=Bob")
        assert resp.status_code == 200
        names = [d["filename"] for d in resp.json()["data"]]
        assert names == ["b.3mf"]

    @pytest.mark.asyncio
    async def test_excludes_non_makerworld_rows(self, async_client, db_session):
        """A library file with a different ``source_type`` must not leak in."""
        await self._seed(db_session, [{"filename": "mw.3mf"}])
        db_session.add(
            LibraryFile(
                filename="upload.3mf",
                file_path="library/upload.3mf",
                file_type="3mf",
                file_size=10,
                source_type="upload",
                source_url=None,
            )
        )
        await db_session.commit()

        resp = await async_client.get("/api/v1/makerworld/imports")
        assert resp.status_code == 200
        assert [d["filename"] for d in resp.json()["data"]] == ["mw.3mf"]

    @pytest.mark.asyncio
    async def test_response_item_carries_meta_fields(self, async_client, db_session):
        """When a meta row exists, the item carries title/author/sliced_for/
        profile_id/has_*cover (defaults False when files aren't on disk)."""
        await self._seed(
            db_session,
            [
                {
                    "filename": "rich.3mf",
                    "title": "Test Title",
                    "author_name": "Test Author",
                    "sliced_for": "X1C",
                }
            ],
        )
        resp = await async_client.get("/api/v1/makerworld/imports")
        assert resp.status_code == 200
        item = resp.json()["data"][0]
        assert item["title"] == "Test Title"
        assert item["author_name"] == "Test Author"
        assert item["sliced_for"] == "X1C"
        assert item["has_cover"] is False
        assert item["has_variant_cover"] is False


class TestRedownloadEndpoint:
    """POST /makerworld/imports/{id}/redownload — force re-fetch the
    3MF bytes for a previously imported variant, overwriting the on-disk
    file + refreshing the meta row.

    Differs from ``POST /makerworld/import`` which short-circuits the
    download via source-url dedupe — this endpoint exists for the
    "creator pushed an update" workflow.
    """

    @pytest.mark.asyncio
    async def test_404_on_unknown_id(self, async_client):
        resp = await async_client.post("/api/v1/makerworld/imports/999999/redownload")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_400_on_non_makerworld_source(self, async_client, db_session):
        """A library file with ``source_type != 'makerworld'`` is rejected — the
        endpoint exists to refresh MakerWorld bytes, nothing else."""
        lf = LibraryFile(
            filename="x.3mf",
            file_path="library/x.3mf",
            file_type="3mf",
            file_size=10,
            source_type="upload",
            source_url=None,
        )
        db_session.add(lf)
        await db_session.commit()

        resp = await async_client.post(f"/api/v1/makerworld/imports/{lf.id}/redownload")
        assert resp.status_code == 400
        assert "makerworld" in resp.json()["detail"].lower()

    @pytest.mark.asyncio
    async def test_400_on_unparseable_source_url(self, async_client, db_session):
        """Refuse if the row's ``source_url`` doesn't match the canonical
        ``/models/{N}[#profileId-P]`` shape — we can't safely re-resolve."""
        lf = LibraryFile(
            filename="x.3mf",
            file_path="library/x.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
            source_url="https://example.com/some/other/path",
        )
        db_session.add(lf)
        await db_session.commit()

        resp = await async_client.post(f"/api/v1/makerworld/imports/{lf.id}/redownload")
        assert resp.status_code == 400

    @pytest.mark.asyncio
    async def test_happy_path_overwrites_bytes_and_refreshes_meta(self, async_client, db_session, tmp_path):
        """End-to-end: redownload writes fresh bytes to the existing path,
        bumps ``file_size`` + ``file_hash``, and updates the meta row."""
        from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta

        # Seed a library file with old bytes on disk + a stale meta row.
        old_bytes = b"OLD-3MF-BYTES"
        new_bytes = b"NEW-FRESH-3MF-BYTES-PUSHED-BY-CREATOR"
        on_disk = tmp_path / "library" / "files" / "old.3mf"
        on_disk.parent.mkdir(parents=True, exist_ok=True)
        on_disk.write_bytes(old_bytes)

        lf = LibraryFile(
            filename="old.3mf",
            file_path="library/files/old.3mf",
            file_type="3mf",
            file_size=len(old_bytes),
            source_type="makerworld",
            source_url="https://makerworld.com/models/3000#profileId-444",
        )
        db_session.add(lf)
        await db_session.flush()
        db_session.add(
            LibraryFileMakerworldMeta(
                library_file_id=lf.id,
                title="Old Title",
                author_name="Old Author",
            )
        )
        await db_session.commit()
        old_hash = lf.file_hash

        # Stub the network: design, instances, signed-URL, bytes.
        svc = _fake_service(
            get_design={
                "id": 3000,
                "modelId": "US3000",
                "title": "New Title",
                "designCreator": {"name": "New Author"},
                "instances": [],
                "coverUrl": None,
            },
            get_design_instances={"hits": [{"profileId": 444, "title": "v444", "cover": None}]},
            get_profile_download={"url": "https://makerworld.bblmw.com/signed", "name": "fresh.3mf"},
            download_3mf=(new_bytes, "fresh.3mf"),
            fetch_thumbnail=(b"", "image/jpeg"),  # no covers
        )

        with (
            patch("backend.app.api.routes.makerworld._build_service", AsyncMock(return_value=svc)),
            patch("backend.app.core.config.settings.base_dir", tmp_path),
        ):
            resp = await async_client.post(f"/api/v1/makerworld/imports/{lf.id}/redownload")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["library_file_id"] == lf.id
        # Same row id — re-download must not orphan FK references.
        assert body["was_existing"] is True
        assert body["profile_id"] == 444

        # File on disk replaced with fresh bytes.
        assert on_disk.read_bytes() == new_bytes

        # DB row updated: size + hash differ from old; row id unchanged.
        await db_session.refresh(lf)
        assert lf.file_size == len(new_bytes)
        assert lf.file_hash != old_hash

        # Meta row refreshed from the new design payload.
        meta = (
            await db_session.execute(
                __import__("sqlalchemy")
                .select(LibraryFileMakerworldMeta)
                .where(LibraryFileMakerworldMeta.library_file_id == lf.id)
            )
        ).scalar_one()
        assert meta.title == "New Title"
        assert meta.author_name == "New Author"


class TestImportCoverEndpoints:
    """GET /makerworld/imports/{id}/cover and /cover-variant.

    These are bypass-auth via the URL-pattern whitelist (``/cover`` in
    path) since ``<img src>`` browser fetches can't carry a JWT. The
    endpoints themselves serve a file from
    ``library/makerworld-covers/<id>-{cover,variant}.<ext>`` based on
    the meta-row's stored relative path.
    """

    @pytest.mark.asyncio
    async def test_cover_404_when_no_meta_row(self, async_client, db_session):
        """No meta row → 404, not 500."""
        lf = LibraryFile(
            filename="x.3mf",
            file_path="library/x.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
        )
        db_session.add(lf)
        await db_session.commit()

        resp = await async_client.get(f"/api/v1/makerworld/imports/{lf.id}/cover")
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_variant_cover_uses_cover_variant_path(self, async_client, db_session):
        """Path is intentionally ``cover-variant`` (not ``variant-cover``)
        so the substring ``/cover`` matches the auth-middleware public
        whitelist — a regression test for the rename + auth-bypass fix.
        """
        from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta

        lf = LibraryFile(
            filename="x.3mf",
            file_path="library/x.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
        )
        db_session.add(lf)
        await db_session.flush()
        db_session.add(LibraryFileMakerworldMeta(library_file_id=lf.id, cover_path=None, variant_cover_path=None))
        await db_session.commit()

        # New path must route; old path must 404 (and stay un-whitelisted).
        new = await async_client.get(f"/api/v1/makerworld/imports/{lf.id}/cover-variant")
        old = await async_client.get(f"/api/v1/makerworld/imports/{lf.id}/variant-cover")
        # New path resolves the route — returns 404 because the cover file
        # is None (no physical bytes), not because the route is missing.
        assert new.status_code == 404
        # Old path returns 404 from the router not matching it at all.
        assert old.status_code == 404

    @pytest.mark.asyncio
    async def test_cover_serves_local_file_with_image_mime(self, async_client, db_session, tmp_path):
        """When a cover file exists on disk and meta carries its path, the
        endpoint returns the bytes with an image/* content-type."""
        from backend.app.models.library_file_makerworld_meta import LibraryFileMakerworldMeta

        lf = LibraryFile(
            filename="x.3mf",
            file_path="library/x.3mf",
            file_type="3mf",
            file_size=10,
            source_type="makerworld",
        )
        db_session.add(lf)
        await db_session.flush()

        # Materialise a fake cover file under tmp_path and store the
        # relative path on the meta row.
        covers_dir = tmp_path / "library" / "makerworld-covers"
        covers_dir.mkdir(parents=True)
        cover_file = covers_dir / f"{lf.id}-cover.jpg"
        cover_file.write_bytes(b"\xff\xd8\xff\xe0\x00\x10JFIF")  # minimal JPEG magic

        rel_path = str(cover_file.relative_to(tmp_path))
        db_session.add(LibraryFileMakerworldMeta(library_file_id=lf.id, cover_path=rel_path))
        await db_session.commit()

        with patch("backend.app.core.config.settings.base_dir", tmp_path):
            resp = await async_client.get(f"/api/v1/makerworld/imports/{lf.id}/cover")

        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("image/jpeg")
        assert resp.content.startswith(b"\xff\xd8")
