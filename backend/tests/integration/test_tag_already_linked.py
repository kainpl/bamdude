"""A tag another active spool holds is refused with one structured 409 in both inventory modes (upstream #3110).

The built-in route named nobody; Spoolman mode named the spool inside a different
sentence. Both now answer ``{"error": "tag_already_linked", "message", "spool_id",
"field"}`` — the code for a client, the translated sentence for a person.

Two active spools really can carry one tag (no unique index; PATCH and bulk create
copy tags without a check), and the built-in lookup read that with
``scalar_one_or_none()``, which raises on two rows. The holder named is the lowest id.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient

from backend.app.models.spool import Spool

TAG = "AABBCCDDEEFF0011"
UUID = "AABBCCDDEEFF0011AABBCCDDEEFF0011"


async def _spool(db, **kw) -> Spool:
    spool = Spool(material="PLA", label_weight=1000, core_weight=250, weight_used=0, **kw)
    db.add(spool)
    await db.commit()
    await db.refresh(spool)
    return spool


@pytest.mark.asyncio
@pytest.mark.integration
async def test_two_active_holders_answer_409_naming_the_lowest(async_client: AsyncClient, db_session):
    first = await _spool(db_session, tag_uid=TAG)
    await _spool(db_session, tag_uid=TAG)  # a second holder: no unique index stops it
    target = await _spool(db_session)

    response = await async_client.patch(f"/api/v1/inventory/spools/{target.id}/link-tag", json={"tag_uid": TAG})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["error"] == "tag_already_linked"
    assert (detail["spool_id"], detail["field"]) == (first.id, "tag_uid")
    assert str(first.id) in detail["message"]


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_tray_uuid_conflict_says_which_identifier(async_client: AsyncClient, db_session):
    holder = await _spool(db_session, tray_uuid=UUID)
    target = await _spool(db_session)

    response = await async_client.patch(f"/api/v1/inventory/spools/{target.id}/link-tag", json={"tray_uuid": UUID})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["error"], detail["spool_id"], detail["field"]) == ("tag_already_linked", holder.id, "tray_uuid")


def _spoolman_spool(spool_id: int, extra: dict) -> dict:
    """A raw Spoolman spool ``_map_spoolman_spool`` accepts."""
    return {
        "id": spool_id,
        "remaining_weight": 800.0,
        "used_weight": 200.0,
        "registered": "2026-09-01T10:00:00",
        "filament": {
            "id": 5,
            "name": "PLA Basic",
            "material": "PLA",
            "color_hex": "FF0000",
            "weight": 1000,
            "spool_weight": 250,
            "vendor": {"id": 1, "name": "Bambu Lab"},
        },
        "extra": extra,
    }


def _client(spools: list[dict]) -> MagicMock:
    client = MagicMock()
    client.get_all_spools = AsyncMock(return_value=spools)
    client.get_spool = AsyncMock(return_value=_spoolman_spool(99, {}))
    client.extra_lock = MagicMock(return_value=__import__("asyncio").Lock())
    return client


@pytest.mark.asyncio
@pytest.mark.integration
async def test_spoolman_mode_answers_the_same_409(async_client: AsyncClient):
    client = _client(
        [
            _spoolman_spool(57, {"tag": json.dumps(UUID)}),
            _spoolman_spool(42, {"tag": json.dumps(UUID)}),
            # Edited outside BamDude: a null tag further down must not take the request down.
            _spoolman_spool(60, {"tag": None}),
            _spoolman_spool(61, {"tag": 12345}),
        ]
    )
    with patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=client)):
        response = await async_client.patch("/api/v1/spoolman/inventory/spools/99/tag", json={"tray_uuid": UUID})

    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert (detail["error"], detail["spool_id"], detail["field"]) == ("tag_already_linked", 42, "tray_uuid")


@pytest.mark.asyncio
@pytest.mark.integration
async def test_a_malformed_tag_elsewhere_does_not_block_a_free_tag(async_client: AsyncClient):
    client = _client([_spoolman_spool(60, {"tag": None}), _spoolman_spool(61, {"tag": ["x"]})])
    client.update_spool_full = AsyncMock(return_value=_spoolman_spool(99, {"tag": json.dumps(UUID)}))
    with (
        patch("backend.app.api.routes.spoolman_inventory._get_client", new=AsyncMock(return_value=client)),
        patch("backend.app.core.websocket.ws_manager.broadcast", new_callable=AsyncMock),
    ):
        response = await async_client.patch("/api/v1/spoolman/inventory/spools/99/tag", json={"tray_uuid": UUID})

    assert response.status_code == 200, response.text
