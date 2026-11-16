"""The socket follows the read rule: a shared external file invalidates for whoever may see it.

A caller with ``library:read_own`` sees a file nobody owns that lives in an
external folder (``models.library.owner_scope_sees``). The live projection asked
its own, older rule — the owner alone — so such a caller saw the file in every
list and heard nothing when a scan added one or somebody else wrote a note.
"""

from __future__ import annotations

import pytest

from backend.app.models.library import LibraryFile
from backend.app.models.user import User
from backend.app.services import live_projection
from backend.app.services.live_authority import Entitlements, live_authority

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

EVENTS = ("library_file_added", "library_file_notes_changed", "library_file_details_changed")
ME = 7


def audience(*permissions) -> Entitlements:
    return Entitlements(frozenset(permissions), None, ME, "account")


async def _shaped(db_session, monkeypatch, kind: str, access: Entitlements, credential_kind: str = "session", **row):
    file = LibraryFile(filename="part.3mf", file_path="/share/part.3mf", file_type="3mf", file_size=1, **row)
    db_session.add(file)
    await db_session.commit()

    async def rows(refs):
        # The authority's own read, so the columns the rule needs are proven to be fetched.
        return await live_authority._metadata(db_session, refs)

    monkeypatch.setattr(live_projection.live_authority, "resource_rows", rows)
    key = "id" if kind == "library_file_added" else "file_id"
    prepared = await live_projection.prepare({"type": kind, "data": {key: file.id}})
    return live_projection.shape(prepared, access, credential_kind=credential_kind), file.id


@pytest.mark.parametrize("kind", EVENTS)
async def test_an_own_reader_hears_about_an_ownerless_external_file(db_session, monkeypatch, kind):
    out, file_id = await _shaped(db_session, monkeypatch, kind, audience("library:read_own"), is_external=True)

    assert out == {"type": kind, "data": {"id" if kind == "library_file_added" else "file_id": file_id}}


@pytest.mark.parametrize("kind", EVENTS)
async def test_an_ownerless_file_in_managed_storage_still_needs_read_all(db_session, monkeypatch, kind):
    out, _ = await _shaped(db_session, monkeypatch, kind, audience("library:read_own"), is_external=False)
    assert out is None


async def test_somebody_else_s_external_file_stays_theirs(db_session, monkeypatch):
    other = User(username="other", password_hash="x", role="user")
    db_session.add(other)
    await db_session.commit()
    assert other.id != ME
    out, _ = await _shaped(
        db_session,
        monkeypatch,
        "library_file_added",
        audience("library:read_own"),
        is_external=True,
        created_by_id=other.id,
    )
    assert out is None


async def test_a_caller_who_reads_no_library_hears_nothing(db_session, monkeypatch):
    out, _ = await _shaped(db_session, monkeypatch, "library_file_added", audience("printers:read"), is_external=True)
    assert out is None


async def test_a_key_with_own_scope_is_still_not_an_owner(db_session, monkeypatch):
    out, _ = await _shaped(
        db_session, monkeypatch, "library_file_added", audience("library:read_own"), "key", is_external=True
    )
    assert out is None


async def test_a_full_reader_hears_as_before(db_session, monkeypatch):
    out, file_id = await _shaped(
        db_session, monkeypatch, "library_file_added", audience("library:read_all"), is_external=False
    )
    assert out == {"type": "library_file_added", "data": {"id": file_id}}
