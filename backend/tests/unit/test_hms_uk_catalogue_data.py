"""The shipped Ukrainian catalogue, checked against the English one it came from.

⚠️ These run on the DATA, not on the pipeline that produced it
(``test_hms_catalogue_translation.py`` covers that). The failure this guards
against is quiet and arrives later: a refresh from a new BambuStudio tag
rewrites the English files, and the Ukrainian ones — which nothing regenerates
automatically — keep describing codes that no longer exist, or drift out of
alignment with the codes that do.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

HMS_DIR = Path(__file__).resolve().parents[2] / "app" / "data" / "hms"
UK_DIR = HMS_DIR / "uk"

UK_FILES = sorted(UK_DIR.glob("*.json")) if UK_DIR.exists() else []
CYRILLIC = re.compile(r"[А-Яа-яЇїІіЄєҐґ]")
# Numbers and units are the part a fluent-sounding translation can silently
# corrupt: "170 °C" becoming "17 °C" reads perfectly well and is dangerous.
FACTS = re.compile(r"\d+(?:\.\d+)?|°C")


@pytest.mark.skipif(not UK_FILES, reason="Ukrainian catalogue not generated")
@pytest.mark.parametrize("uk_path", UK_FILES, ids=lambda p: p.stem)
class TestEveryUkrainianFile:
    def test_it_has_an_english_counterpart(self, uk_path: Path) -> None:
        assert (HMS_DIR / uk_path.name).exists(), "a model we no longer ship English for"

    def test_every_code_it_describes_still_exists(self, uk_path: Path) -> None:
        """⚠️ The direction that rots. A BS refresh can retire codes; the uk file
        is not regenerated with it, so leftovers accumulate and are served to
        operators for faults the firmware can no longer raise."""
        en = json.loads((HMS_DIR / uk_path.name).read_text(encoding="utf-8"))
        uk = json.loads(uk_path.read_text(encoding="utf-8"))

        orphans = sorted(set(uk) - set(en))
        assert not orphans, f"{len(orphans)} codes with no English original, e.g. {orphans[:5]}"

    def test_nothing_is_blank(self, uk_path: Path) -> None:
        """A blank would win the lookup and show the operator nothing at all —
        strictly worse than the English fallback it displaced."""
        uk = json.loads(uk_path.read_text(encoding="utf-8"))

        assert [code for code, text in uk.items() if not text.strip()] == []

    def test_it_is_actually_Ukrainian(self, uk_path: Path) -> None:
        """⚠️ English copied into the uk file is the failure the pipeline is
        built to avoid: it makes a half-finished translation indistinguishable
        from a complete one, and the lookup already falls back to English on its
        own. A handful of entries are legitimately identifiers only."""
        uk = json.loads(uk_path.read_text(encoding="utf-8"))
        latin_only = [t for t in uk.values() if len(t) > 25 and not CYRILLIC.search(t)]

        assert len(latin_only) <= 2, f"{len(latin_only)} untranslated entries, e.g. {latin_only[:3]}"

    def test_numbers_and_units_survived_translation(self, uk_path: Path) -> None:
        en = json.loads((HMS_DIR / uk_path.name).read_text(encoding="utf-8"))
        uk = json.loads(uk_path.read_text(encoding="utf-8"))

        drifted = [code for code, text in uk.items() if sorted(FACTS.findall(en[code])) != sorted(FACTS.findall(text))]
        assert not drifted, f"{len(drifted)} entries whose numbers changed, e.g. {drifted[:5]}"
