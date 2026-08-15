"""Looking an HMS description up by model and code.

⚠️ The model is the serial number's first three characters, exactly as
``hms_actions.get_actions_for_error_code`` does it. The two data files are keyed
the same way on purpose: one rule for a reader to learn, and the pair of
questions — "what is it" and "what can I do about it" — answered from the same
place.
"""

from __future__ import annotations

import json

import pytest

from backend.app.services import hms_catalogue


@pytest.fixture(autouse=True)
def catalogue(tmp_path, monkeypatch):
    """A two-model, two-language catalogue on disk, in the real layout."""
    (tmp_path / "uk").mkdir()
    (tmp_path / "20P.json").write_text(
        json.dumps({"050002000002000B": "Long form", "0580409C": "Short form"}), encoding="utf-8"
    )
    (tmp_path / "31B.json").write_text(
        json.dumps({"050002000002000B": "Another machine, another mechanism"}), encoding="utf-8"
    )
    (tmp_path / "uk" / "20P.json").write_text(json.dumps({"050002000002000B": "Довга форма"}), encoding="utf-8")
    monkeypatch.setattr(hms_catalogue, "_DATA_DIR", tmp_path)
    hms_catalogue._CATALOGUES.clear()
    yield
    hms_catalogue._CATALOGUES.clear()


class TestFindingOneDescription:
    def test_the_full_code_is_tried_first(self) -> None:
        """It is lossless. The short code collapses information and can collide."""
        assert hms_catalogue.describe("20P", "050002000002000B", "0580409C") == "Long form"

    def test_the_short_code_is_the_fallback(self) -> None:
        assert hms_catalogue.describe("20P", "FFFFFFFFFFFFFFFF", "0580409C") == "Short form"

    def test_either_code_may_be_absent(self) -> None:
        assert hms_catalogue.describe("20P", None, "0580409C") == "Short form"
        assert hms_catalogue.describe("20P", "050002000002000B", None) == "Long form"

    def test_an_uncatalogued_code_answers_none(self) -> None:
        """``None``, not ``""`` — the UI shows its own "unrecognised" text, and
        an empty string would render as a blank line instead."""
        assert hms_catalogue.describe("20P", "DEADBEEFDEADBEEF", "DEADBEEF") is None

    def test_an_unknown_model_does_not_borrow_another_models_text(self) -> None:
        """⚠️ 879 codes mean different things on different machines. Answering
        from a model we happen to have loaded would describe the wrong
        mechanism, confidently."""
        assert hms_catalogue.describe("ZZZ", "050002000002000B", None) is None

    def test_each_model_gets_its_own_text_for_the_same_code(self) -> None:
        """The reason the catalogue is per model at all."""
        assert hms_catalogue.describe("20P", "050002000002000B", None) == "Long form"
        assert hms_catalogue.describe("31B", "050002000002000B", None) == "Another machine, another mechanism"


class TestLanguage:
    def test_a_translated_code_answers_in_that_language(self) -> None:
        assert hms_catalogue.describe("20P", "050002000002000B", None, "uk") == "Довга форма"

    def test_an_untranslated_code_falls_back_to_english(self) -> None:
        """⚠️ Not to nothing. A description in the wrong language beats a blank
        where a fault should be — and BambuStudio ships no Ukrainian at all, so
        this is the common case, not the edge one."""
        assert hms_catalogue.describe("20P", None, "0580409C", "uk") == "Short form"

    def test_a_language_we_have_no_files_for_still_answers(self) -> None:
        assert hms_catalogue.describe("20P", "050002000002000B", None, "de") == "Long form"


class TestTheWholeCatalogueForOneModel:
    def test_english_is_filled_in_under_a_translation(self) -> None:
        result = hms_catalogue.descriptions_for("20P", "uk")

        assert result["050002000002000B"] == "Довга форма"  # translated
        assert result["0580409C"] == "Short form"  # not yet translated

    def test_english_alone_is_returned_as_is(self) -> None:
        assert hms_catalogue.descriptions_for("20P") == {
            "050002000002000B": "Long form",
            "0580409C": "Short form",
        }

    def test_an_unknown_model_is_empty_rather_than_an_error(self) -> None:
        assert hms_catalogue.descriptions_for("ZZZ") == {}


def test_a_missing_file_is_cached_as_empty_rather_than_reread_every_call() -> None:
    """The files are ~700 KB each and a farm asks about the same two models all
    day. A miss must not turn into a filesystem hit per error."""
    hms_catalogue.describe("ZZZ", "AAAA", None)
    assert ("en", "ZZZ") in hms_catalogue._CATALOGUES
