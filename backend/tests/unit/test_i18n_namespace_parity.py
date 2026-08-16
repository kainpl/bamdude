"""Every backend translation namespace carries the same keys in en and uk.

The rule is "keys in BOTH", and until now nothing checked it on the backend —
the frontend has `keysResolve`, the backend had nothing. A missing key here is
quiet in a specific way: ``t()`` falls back to English on a miss and returns
the raw KEY when English lacks it too, so the failure surfaces either as
English mid-Ukrainian text or as ``filament_runout`` printed at an operator.

⚠️ The check is on the key SET, not on whether a value is non-empty. A key
present in both files with an untranslated English value is a translation
decision (some strings are identical in both languages); a key present in one
file only is always a bug.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

DATA_DIR = Path(__file__).resolve().parents[2] / "app" / "data"
NAMESPACES = sorted(p.name[: -len("_en.json")] for p in DATA_DIR.glob("*_en.json"))


def _keys(payload, prefix: str = "") -> set[str]:
    if isinstance(payload, dict):
        out: set[str] = set()
        for k, v in payload.items():
            out |= _keys(v, f"{prefix}.{k}" if prefix else k)
        return out
    return {prefix}


def _load(namespace: str, lang: str) -> dict:
    path = DATA_DIR / f"{namespace}_{lang}.json"
    assert path.exists(), f"{path.name} is missing — en+uk only, and both are required"
    return json.loads(path.read_text(encoding="utf-8"))


def test_there_is_at_least_one_namespace_to_check():
    """Guards the test itself: a glob that matches nothing passes silently."""
    assert NAMESPACES, f"no *_en.json under {DATA_DIR}"


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_ukrainian_carries_every_english_key(namespace: str):
    en, uk = _keys(_load(namespace, "en")), _keys(_load(namespace, "uk"))

    assert not (en - uk), f"{namespace}: missing from uk — {sorted(en - uk)[:10]}"


@pytest.mark.parametrize("namespace", NAMESPACES)
def test_ukrainian_carries_no_key_english_lacks(namespace: str):
    """A uk-only key is dead weight: ``t()`` is called with keys the code
    knows, and the code is written against the English file."""
    en, uk = _keys(_load(namespace, "en")), _keys(_load(namespace, "uk"))

    assert not (uk - en), f"{namespace}: only in uk — {sorted(uk - en)[:10]}"
