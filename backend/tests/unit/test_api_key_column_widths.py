"""What generate_api_key() produces must fit the api_keys columns on PostgreSQL,
which enforces VARCHAR lengths where SQLite never did (found 2026-09-07:
key_hash was VARCHAR(64) for an 87-character pbkdf2 hash)."""

from backend.app.core.auth import generate_api_key
from backend.app.models.api_key import APIKey


def test_generated_hash_and_prefix_fit_their_columns():
    _full_key, key_hash, key_prefix = generate_api_key()
    assert len(key_hash) <= APIKey.__table__.c.key_hash.type.length
    assert len(key_prefix) <= APIKey.__table__.c.key_prefix.type.length


def test_the_columns_are_wide_enough_for_pbkdf2_and_the_bb_prefix():
    # passlib's pbkdf2_sha256 string is ~87 characters; the prefix is "bb_" + 8
    assert APIKey.__table__.c.key_hash.type.length >= 128
    assert APIKey.__table__.c.key_prefix.type.length >= 11
