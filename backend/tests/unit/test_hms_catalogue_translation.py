"""Ukrainian descriptions, and the deduplication that makes them affordable.

⚠️ **Deduplicate for TRANSLATION, never for storage.** The stored files stay
per-model and un-deduplicated — 879 codes carry different text on different
machines, so a merged catalogue would describe the wrong mechanism. But the same
sentence repeats across up to seven models: 36 522 stored strings are only 6 378
distinct ones, so translating the distinct set and expanding it back is a fifth
of the work for an identical result.

BambuStudio ships sixteen languages and Ukrainian is not among them, so this one
is ours to make.
"""

from __future__ import annotations

from scripts.translate_hms_catalogue import distinct_strings, expand


class TestCollectingWhatNeedsTranslating:
    def test_each_distinct_string_appears_once(self) -> None:
        catalogues = {
            "20P": {"AAAA": "Nozzle clogged", "BBBB": "Bed not level"},
            "31B": {"CCCC": "Nozzle clogged"},
        }

        assert distinct_strings(catalogues) == {"Bed not level", "Nozzle clogged"}

    def test_nothing_to_translate_is_an_empty_set_not_an_error(self) -> None:
        assert distinct_strings({}) == set()
        assert distinct_strings({"20P": {}}) == set()


class TestPuttingThemBack:
    def test_a_translation_lands_under_every_code_that_used_it(self) -> None:
        """The expansion half of the trade: one translation, seven models."""
        catalogues = {"20P": {"AAAA": "Nozzle clogged"}, "31B": {"CCCC": "Nozzle clogged"}}
        translations = {"Nozzle clogged": "Сопло забите"}

        assert expand(catalogues, translations) == {
            "20P": {"AAAA": "Сопло забите"},
            "31B": {"CCCC": "Сопло забите"},
        }

    def test_an_untranslated_string_is_left_out_rather_than_left_in_english(self) -> None:
        """⚠️ The lookup already falls back to English, so an untranslated code
        resolves there anyway. Copying English into the uk file would hide which
        strings still need work — and make a half-finished translation look
        complete."""
        catalogues = {"20P": {"AAAA": "Nozzle clogged", "BBBB": "Bed not level"}}
        translations = {"Nozzle clogged": "Сопло забите"}

        assert expand(catalogues, translations) == {"20P": {"AAAA": "Сопло забите"}}

    def test_a_model_with_nothing_translated_yields_no_file_at_all(self) -> None:
        """An empty uk file would be indistinguishable from a translated one
        with no matches — and would be loaded and cached for nothing."""
        catalogues = {"20P": {"AAAA": "Nozzle clogged"}, "31B": {"CCCC": "Bed not level"}}
        translations = {"Nozzle clogged": "Сопло забите"}

        assert expand(catalogues, translations) == {"20P": {"AAAA": "Сопло забите"}}

    def test_a_blank_translation_is_not_a_translation(self) -> None:
        """The work file starts as every string mapped to "". A half-filled one
        must not write blanks over the English fallback."""
        catalogues = {"20P": {"AAAA": "Nozzle clogged", "BBBB": "Bed not level"}}
        translations = {"Nozzle clogged": "Сопло забите", "Bed not level": "   "}

        assert expand(catalogues, translations) == {"20P": {"AAAA": "Сопло забите"}}
