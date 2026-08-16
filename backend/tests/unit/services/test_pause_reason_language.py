"""A pause reason is described in the system language, not always in English.

Reported from a live farm: the Telegram pause notification read

    Друк на паузі
    Причина: Please observe the nozzle. If the filament has been extruded...

The title was Ukrainian and the reason was not. The catalogue held the
Ukrainian text all along — ``hms_errors._describe`` simply called
``hms_catalogue.describe`` without ``lang``, so the parameter's ``"en"``
default answered for it. The same catalogue served the web error dialog in
Ukrainian correctly, because that caller did pass it.

⚠️ **This could not be fixed by passing a language down from the caller.**
``classify_pause_reason`` runs on the synchronous MQTT push path, and reading
the setting is an async DB call. Hence the process-memory cache — and hence
the tests below on staleness, which is the only thing that cache can get
wrong.
"""

from __future__ import annotations

import pytest

from backend.app import i18n
from backend.app.services.hms_errors import classify_pause_reason

# 0300_8004 → filament_runout; described per model in the shipped catalogue.
RUNOUT = "0300_8004"
DEVICE = "01P"  # P1S — one of the seven BambuStudio does not package


@pytest.fixture(autouse=True)
def _restore_language_cache():
    before = i18n.current_language()
    yield
    i18n.set_language_cache(before)


class TestTheLanguageReachesTheDescription:
    def test_ukrainian_is_used_when_the_system_is_ukrainian(self):
        i18n.set_language_cache("uk")

        _key, label, _code = classify_pause_reason([RUNOUT], device=DEVICE)

        assert any("Ѐ" <= ch <= "ӿ" for ch in label), f"expected Cyrillic, got {label!r}"

    def test_english_is_used_when_the_system_is_english(self):
        i18n.set_language_cache("en")

        _key, label, _code = classify_pause_reason([RUNOUT], device=DEVICE)

        assert not any("Ѐ" <= ch <= "ӿ" for ch in label), f"expected English, got {label!r}"

    def test_the_two_languages_actually_differ(self):
        """Guards the shape of the test itself: if the catalogue lookup broke
        and both fell through to the same generic label, the two assertions
        above could still both hold."""
        i18n.set_language_cache("uk")
        _, uk_label, _ = classify_pause_reason([RUNOUT], device=DEVICE)
        i18n.set_language_cache("en")
        _, en_label, _ = classify_pause_reason([RUNOUT], device=DEVICE)

        assert uk_label != en_label

    def test_an_explicit_language_overrides_the_cache(self):
        i18n.set_language_cache("en")

        _key, label, _code = classify_pause_reason([RUNOUT], device=DEVICE, lang="uk")

        assert any("Ѐ" <= ch <= "ӿ" for ch in label)

    def test_an_untranslated_language_falls_back_to_english_not_to_blank(self):
        i18n.set_language_cache("de")

        _key, label, _code = classify_pause_reason([RUNOUT], device=DEVICE)

        assert label and not any("Ѐ" <= ch <= "ӿ" for ch in label)


class TestTheGenericLabels:
    """The reasons BamDude raises itself — no HMS code, no catalogue entry."""

    def test_an_internal_pause_reason_is_translated(self):
        i18n.set_language_cache("uk")

        key, label, code = classify_pause_reason(None, expected_reason="plate_objects")

        assert (key, code) == ("plate_objects", None)
        assert any("Ѐ" <= ch <= "ӿ" for ch in label), f"expected Cyrillic, got {label!r}"

    def test_the_no_information_case_is_translated_too(self):
        i18n.set_language_cache("uk")

        _key, label, _code = classify_pause_reason(None)

        assert any("Ѐ" <= ch <= "ӿ" for ch in label), f"expected Cyrillic, got {label!r}"

    def test_english_stays_english(self):
        i18n.set_language_cache("en")

        _key, label, _code = classify_pause_reason(None, expected_reason="plate_objects")

        assert label == "Objects detected on plate"

    def test_a_key_missing_from_the_file_falls_back_to_prose_not_to_the_key(self):
        """⚠️ ``t()`` returns the raw key on a miss. Showing an operator
        "filament_runout" is worse than showing them English."""
        from backend.app.services.hms_errors import PAUSE_REASON_LABELS, _label

        assert _label("unknown", "uk") != "unknown"
        # A key no locale file has at all still resolves to the English table.
        PAUSE_REASON_LABELS["_probe"] = "Probe label"
        try:
            assert _label("_probe", "uk") == "Probe label"
        finally:
            PAUSE_REASON_LABELS.pop("_probe", None)


class TestTheCache:
    def test_a_cold_process_answers_english_rather_than_raising(self):
        i18n.set_language_cache(None)

        assert i18n.current_language() == "en"

    def test_switching_the_language_takes_effect_without_a_restart(self):
        """``locale_updater`` stamps the new value at the write. Before this,
        a sync caller had no way to learn the language had changed at all."""
        i18n.set_language_cache("en")
        _, before, _ = classify_pause_reason([RUNOUT], device=DEVICE)

        i18n.set_language_cache("uk")
        _, after, _ = classify_pause_reason([RUNOUT], device=DEVICE)

        assert before != after

    @pytest.mark.asyncio
    async def test_a_failed_read_keeps_the_known_language_instead_of_reverting(self, monkeypatch):
        """⚠️ The failure mode this guards is silent: a transient DB error
        during ``get_language`` used to return ``"en"``, and with the cache in
        place that answer would have been written down as the truth."""
        i18n.set_language_cache("uk")

        def _explode(*_a, **_k):
            raise RuntimeError("database is gone")

        monkeypatch.setattr("backend.app.core.database.async_session", _explode)

        assert await i18n.get_language() == "uk"
        assert i18n.current_language() == "uk"
