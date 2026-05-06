"""Unit tests for m046's `_normalize` helper.

These exercise the pure list-rewrite logic without needing a live DB.
Migration end-to-end behaviour is verified separately in the
integration test (``test_normalize_group_permissions_migration.py``).
"""

from __future__ import annotations

import pytest

from backend.app.migrations.m046_normalize_group_permissions import _normalize


class TestNormalizeGroupPermissions:
    """Pin the rename / drop-unknown / dedupe contract."""

    def test_renames_filaments_to_inventory(self):
        valid = {"inventory:read", "inventory:create", "inventory:update", "inventory:delete"}
        result = _normalize(
            ["filaments:read", "filaments:create", "filaments:update", "filaments:delete"],
            valid,
        )
        assert result == ["inventory:read", "inventory:create", "inventory:update", "inventory:delete"]

    def test_renames_github_to_git(self):
        valid = {"git:backup", "git:restore"}
        result = _normalize(["github:backup", "github:restore"], valid)
        assert result == ["git:backup", "git:restore"]

    def test_drops_unknown_keys(self):
        # ``some:legacy_perm`` is not in valid and not in the rename map → dropped.
        valid = {"inventory:read"}
        result = _normalize(["inventory:read", "some:legacy_perm", "another:gone"], valid)
        assert result == ["inventory:read"]

    def test_dedupes_collisions(self):
        # Group somehow ended up with both old and new key — collapse to one.
        # First occurrence wins for ordering.
        valid = {"inventory:read"}
        result = _normalize(["filaments:read", "inventory:read"], valid)
        assert result == ["inventory:read"]

    def test_dedupes_after_rename(self):
        # New key first, old key second → still single canonical entry.
        valid = {"inventory:read"}
        result = _normalize(["inventory:read", "filaments:read"], valid)
        assert result == ["inventory:read"]

    def test_preserves_order_of_first_occurrence(self):
        # Operators have muscle memory for permission ordering — the list
        # must not reshuffle on upgrade.
        valid = {"inventory:read", "git:backup", "queue:read"}
        result = _normalize(["queue:read", "filaments:read", "github:backup"], valid)
        assert result == ["queue:read", "inventory:read", "git:backup"]

    def test_idempotent_on_clean_input(self):
        # Re-running on already-current keys must produce identical output.
        valid = {"inventory:read", "git:backup", "queue:read"}
        clean = ["queue:read", "inventory:read", "git:backup"]
        assert _normalize(clean, valid) == clean
        # And again — same list every time.
        assert _normalize(_normalize(clean, valid), valid) == clean

    def test_empty_list_returns_empty(self):
        assert _normalize([], {"inventory:read"}) == []

    def test_skips_non_string_entries(self):
        # Defensive: a corrupted JSON list with a None or int doesn't crash
        # the upgrade — non-strings are silently dropped.
        valid = {"inventory:read"}
        result = _normalize(["inventory:read", None, 42, "filaments:read"], valid)  # type: ignore[list-item]
        assert result == ["inventory:read"]

    def test_filament_collision_with_existing_inventory(self):
        # A custom group might have been built post-rename but accidentally
        # imported a backup that re-introduced filaments:read. After
        # normalisation the inventory:* set must stay intact, no dupes.
        valid = {"inventory:read", "inventory:create"}
        result = _normalize(
            ["inventory:read", "inventory:create", "filaments:read", "filaments:create"],
            valid,
        )
        assert result == ["inventory:read", "inventory:create"]


@pytest.mark.parametrize(
    "old_key,expected_new",
    [
        ("filaments:read", "inventory:read"),
        ("filaments:create", "inventory:create"),
        ("filaments:update", "inventory:update"),
        ("filaments:delete", "inventory:delete"),
        ("github:backup", "git:backup"),
        ("github:restore", "git:restore"),
    ],
)
def test_each_rename_pair(old_key, expected_new):
    """Every documented rename pair maps cleanly. If a future PR removes
    one of these mappings the corresponding row here must be removed too —
    parametrised to make the link obvious."""
    valid = {expected_new}
    assert _normalize([old_key], valid) == [expected_new]
