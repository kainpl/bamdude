"""Every sentence the API can refuse with has its Ukrainian — the catalog cannot rot silently.

``backend/app/i18n/api_errors.py`` translates ``HTTPException`` details at the
boundary by looking the English sentence up in ``data/api_errors_uk.json``. A
sentence that is not in the catalog goes out in English, which the user sees
as a strip of the wrong language in a toast — and nothing else would ever
report it. So the check is a test: ``scripts/api_error_catalog.py`` walks the
AST for everything that can reach the wire, and this test holds the catalog
to it, both ways (no missing sentence, no stale key).

Failing here means: run ``python scripts/api_error_catalog.py sync`` and fill
in the empty values (or ``sync --prune`` for a sentence that was removed).
"""

import re
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import api_error_catalog as catalog_tool  # noqa: E402

from backend.app.i18n.api_errors import MACHINE_READ, is_code  # noqa: E402

_INDEXED = re.compile(r"\{(\d+)\}")


@pytest.fixture(scope="module")
def found() -> dict[str, set[catalog_tool.Site]]:
    return catalog_tool.scan()


@pytest.fixture(scope="module")
def catalog() -> dict[str, str]:
    return catalog_tool.load_catalog()


def test_the_scan_finds_the_sentences_it_is_meant_to(found):
    """Guards the guard: an extractor that matched nothing would pass every other test."""
    assert "Printer not found" in found
    kinds = {site.kind for sites in found.values() for site in sites}
    assert {"http", "json", "forwarded", "value"} <= kinds


def test_every_required_sentence_has_ukrainian(found, catalog):
    required = catalog_tool.required_sentences(found, MACHINE_READ)
    missing = sorted(s for s in required if not catalog.get(s))
    assert not missing, (
        f"{len(missing)} API sentences have no Ukrainian — run scripts/api_error_catalog.py sync:\n"
        + "\n".join(f"  {s!r}" for s in missing[:20])
    )


def test_the_catalog_carries_no_stale_key(found, catalog):
    """A key nobody raises any more is a translation nobody will ever see — and a sign the sync was skipped."""
    orphans = sorted(k for k in catalog if k not in found)
    assert not orphans, f"stale catalog keys (sync --prune): {orphans[:10]}"


def test_no_catalog_value_is_empty(catalog):
    empty = sorted(k for k, v in catalog.items() if not v or not v.strip())
    assert not empty, f"empty translations: {empty[:10]}"


def test_placeholders_match_between_english_and_ukrainian(catalog):
    """``{}`` count must agree, or the handler puts a value in the wrong place — or drops it."""
    bad = []
    for key, value in catalog.items():
        wanted = key.count(catalog_tool.PLACEHOLDER)
        indexed = {int(m) for m in _INDEXED.findall(value)}
        got = len(indexed) if indexed else value.count(catalog_tool.PLACEHOLDER)
        if indexed and (indexed != set(range(wanted)) or catalog_tool.PLACEHOLDER in value) or got != wanted:
            bad.append((key, value))
    assert not bad, "placeholder mismatch:\n" + "\n".join(f"  {k!r} -> {v!r}" for k, v in bad[:10])


def test_machine_read_sentences_stay_out_of_the_catalog(catalog):
    """The frontend branches on these; a translation would break the auth flow."""
    leaked = sorted(k for k in catalog if k in MACHINE_READ)
    assert not leaked, leaked


def test_machine_read_sentences_are_still_raised_somewhere(found):
    """An allowlist entry nobody raises is a stale allowlist — the frontend list it mirrors has moved on."""
    stale = sorted(s for s in MACHINE_READ if s not in found)
    assert not stale, stale


def test_codes_are_never_treated_as_sentences(found, catalog):
    codes = sorted(k for k in list(found) + list(catalog) if is_code(k))
    assert not codes, codes
