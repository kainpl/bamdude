"""``backend/app/i18n/api_errors.py`` — refusals answered in the system language, at the boundary.

Exercises the translation against a throwaway catalog (so the tests pin the
mechanism, not today's wording), the exception handler on a small app (status
and headers must survive the swap), and the one property the real app must
have: the translating handler is the one installed.
"""

import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from backend.app.i18n import api_errors, set_language_cache

CATALOG = {
    "Printer not found": "Принтер не знайдено",
    "Printer {} not found": "Принтер {} не знайдено",
    "Printer {} not found in {}": "У {1} немає принтера {0}",
    "Spool created but slot assignment failed: {}": "Котушку створено, але призначити слот не вдалося: {}",
    "Spool created but slot assignment failed: {} ({})": "Котушку створено, але призначити слот не вдалося: {} ({})",
}


@pytest.fixture(autouse=True)
def _catalog(tmp_path, monkeypatch):
    (tmp_path / "api_errors_uk.json").write_text(json.dumps(CATALOG, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(api_errors, "DATA_DIR", tmp_path)
    api_errors.reload_catalog()
    yield
    api_errors.reload_catalog()


@pytest.fixture
def ukrainian():
    set_language_cache("uk")
    yield
    set_language_cache("en")


# --- translate_sentence ------------------------------------------------------


def test_an_exact_sentence_is_translated():
    assert api_errors.translate_sentence("Printer not found", "uk") == "Принтер не знайдено"


def test_english_is_left_alone_even_when_a_translation_exists():
    assert api_errors.translate_sentence("Printer not found", "en") == "Printer not found"


def test_an_unknown_sentence_goes_out_unchanged():
    assert api_errors.translate_sentence("Nobody wrote this one down", "uk") == "Nobody wrote this one down"


def test_a_template_captures_the_value_and_puts_it_back():
    assert api_errors.translate_sentence("Printer 42 not found", "uk") == "Принтер 42 не знайдено"


def test_the_longer_template_wins_over_its_prefix():
    """``…: {}`` would also match the two-value sentence; the more specific template is tried first."""
    text = "Spool created but slot assignment failed: timeout (tray 3)"
    assert (
        api_errors.translate_sentence(text, "uk")
        == "Котушку створено, але призначити слот не вдалося: timeout (tray 3)"
    )


def test_indexed_placeholders_reorder_the_values():
    assert api_errors.translate_sentence("Printer P1S not found in Kitchen", "uk") == "У Kitchen немає принтера P1S"


def test_a_captured_value_may_span_lines_and_contain_braces():
    text = "Printer {weird}\nname not found"
    assert api_errors.translate_sentence(text, "uk") == "Принтер {weird}\nname не знайдено"


@pytest.mark.parametrize("sentence", sorted(api_errors.MACHINE_READ))
def test_machine_read_sentences_are_never_translated(sentence, tmp_path):
    """Even with a translation in the catalog — the frontend branches on the English text."""
    (tmp_path / "api_errors_uk.json").write_text(
        json.dumps({sentence: "переклад"}, ensure_ascii=False), encoding="utf-8"
    )
    api_errors.reload_catalog()
    assert api_errors.translate_sentence(sentence, "uk") == sentence


def test_a_code_is_never_translated(tmp_path):
    (tmp_path / "api_errors_uk.json").write_text(json.dumps({"parts_not_editable": "x"}), encoding="utf-8")
    api_errors.reload_catalog()
    assert api_errors.is_code("parts_not_editable")
    assert not api_errors.is_code("Printer not found")
    assert api_errors.translate_sentence("parts_not_editable", "uk") == "parts_not_editable"


def test_a_missing_catalog_means_english(tmp_path, monkeypatch):
    monkeypatch.setattr(api_errors, "DATA_DIR", tmp_path / "nowhere")
    api_errors.reload_catalog()
    assert api_errors.translate_sentence("Printer not found", "uk") == "Printer not found"


# --- translate_detail --------------------------------------------------------


def test_a_dict_detail_has_only_its_message_translated():
    detail = {"error": "not_sliced", "message": "Printer not found", "extra": 1}
    assert api_errors.translate_detail(detail, "uk") == {
        "error": "not_sliced",
        "message": "Принтер не знайдено",
        "extra": 1,
    }
    assert detail["message"] == "Printer not found", "the caller's dict is not mutated"


def test_a_list_detail_passes_through():
    detail = [{"loc": ["body", "x"], "msg": "field required"}]
    assert api_errors.translate_detail(detail, "uk") is detail


# --- the handler on an app ---------------------------------------------------


def _app() -> FastAPI:
    app = FastAPI()
    api_errors.install(app)

    @app.get("/missing")
    async def missing():
        raise HTTPException(404, "Printer not found")

    @app.get("/auth")
    async def auth():
        raise HTTPException(401, "Printer not found", headers={"WWW-Authenticate": "Bearer"})

    @app.get("/structured")
    async def structured():
        raise HTTPException(404, {"error": "not_sliced", "message": "Printer not found"})

    @app.get("/hand-built")
    async def hand_built():
        return api_errors.json_error(400, "Printer 7 not found")

    return app


def test_the_handler_translates_when_the_system_language_is_ukrainian(ukrainian):
    response = TestClient(_app()).get("/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "Принтер не знайдено"}


def test_the_handler_leaves_english_systems_byte_identical():
    set_language_cache("en")
    response = TestClient(_app()).get("/missing")
    assert response.status_code == 404
    assert response.json() == {"detail": "Printer not found"}


def test_status_and_headers_survive_the_swap(ukrainian):
    response = TestClient(_app()).get("/auth")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"
    assert response.json() == {"detail": "Принтер не знайдено"}


def test_a_structured_detail_keeps_its_code_and_translates_its_message(ukrainian):
    response = TestClient(_app()).get("/structured")
    assert response.json() == {"detail": {"error": "not_sliced", "message": "Принтер не знайдено"}}


def test_a_hand_built_refusal_speaks_the_system_language_too(ukrainian):
    response = TestClient(_app()).get("/hand-built")
    assert response.status_code == 400
    assert response.json() == {"detail": "Принтер 7 не знайдено"}


def test_the_real_app_has_the_translating_handler_installed():
    from backend.app.main import app

    assert app.exception_handlers[StarletteHTTPException] is api_errors._handle
