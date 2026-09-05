"""API refusals answered in the system language — translated once, at the boundary.

The API refuses with an English sentence in ``detail`` (``HTTPException(409,
detail="...")``), written where the refusal is decided — some seven hundred of
them. The interface shows that sentence verbatim in a toast, so on a Ukrainian
system it read as a strip of English in the middle of the user's own language.

Translation happens *here*, in the exception handler, gettext-style: the English
sentence is the key, ``data/api_errors_uk.json`` carries the Ukrainian, and the
raise sites never change. When the system language is English nothing is
touched; when a sentence is missing from the catalog it goes out in English —
never as a bare key. An f-string is a template with ``{}`` where the value goes
(``"Printer {} not found"``), matched back against the formatted sentence with a
regex; the Ukrainian side may reorder the values with ``{0}``/``{1}``.

The language is the **system** language (``settings.language`` via the warm
``current_language()`` cache), the same authority the interface, notifications
and Telegram follow — never ``Accept-Language``. A third-party client on a
Ukrainian server therefore gets Ukrainian, exactly as it already gets Ukrainian
notifications.

⚠️ A handful of sentences are *read by the frontend*, not shown: the auth
refresh logic in ``api/client.ts`` decides "refresh or log out" by the 401 text.
Those are ``MACHINE_READ`` and are never translated — they are also kept out of
the catalog by ``scripts/api_error_catalog.py``. Everything the frontend needs to
branch on beyond that travels as a structured ``{"error": code, "message": ...}``
detail, where only ``message`` is translated.

Coverage is a test, not a promise: ``tests/unit/test_api_error_details_have_ukrainian.py``
walks the AST for every sentence that can reach the wire and fails when one has
no Ukrainian. ``python scripts/api_error_catalog.py sync`` adds the missing keys.
"""

from __future__ import annotations

import json
import logging
import re
from functools import lru_cache
from typing import Any

from fastapi import FastAPI
from fastapi.exception_handlers import http_exception_handler
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from backend.app.i18n import DATA_DIR, FALLBACK_LANG, current_language

logger = logging.getLogger(__name__)

PLACEHOLDER = "{}"

# Sentences the frontend branches on rather than shows. Translating one would
# silently break the auth flow (a refreshable 401 would be treated as a real
# logout, or the reverse). Grep ``REFRESH_ERROR_MESSAGES`` /
# ``NON_REFRESHABLE_401_MESSAGES`` in ``frontend/src/api/client.ts`` and the
# setup gate / auth middleware in ``main.py`` before touching this set.
MACHINE_READ: frozenset[str] = frozenset(
    {
        "Could not validate credentials",
        "Token has expired",
        "Invalid token",
        "User not found or inactive",
        "Invalid API key",
        "API key has expired",
        "Authentication required",
        # ``setup_required`` and the like need no entry: a bare code is never a sentence (``is_code``).
    }
)

_INDEXED = re.compile(r"\{(\d+)\}")
_CODE = re.compile(r"^[a-z0-9_]+$")


def is_code(text: str) -> bool:
    """A ``detail`` like ``parts_not_editable`` is a code the frontend maps itself — never a sentence to translate."""
    return bool(_CODE.match(text))


def _compile(template: str) -> re.Pattern[str]:
    parts = template.split(PLACEHOLDER)
    pattern = "".join(re.escape(part) + ("(.*?)" if i < len(parts) - 1 else "") for i, part in enumerate(parts))
    return re.compile(pattern, re.DOTALL)


def _fill(template: str, values: tuple[str, ...]) -> str:
    """Put the captured values into the translated template — ``{0}``/``{1}`` reorder, ``{}`` runs in order."""
    if _INDEXED.search(template):
        return _INDEXED.sub(
            lambda m: values[int(m.group(1))] if int(m.group(1)) < len(values) else m.group(0), template
        )
    out: list[str] = []
    for i, part in enumerate(template.split(PLACEHOLDER)):
        out.append(part)
        if i < len(values):
            out.append(values[i])
    return "".join(out)


@lru_cache(maxsize=4)
def _catalog(lang: str) -> tuple[dict[str, str], tuple[tuple[re.Pattern[str], str], ...]]:
    path = DATA_DIR / f"api_errors_{lang}.json"
    try:
        raw: dict[str, str] = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        logger.warning("No API error catalog for %r at %s — refusals stay in English", lang, path)
        return {}, ()
    exact: dict[str, str] = {}
    templates: list[tuple[re.Pattern[str], str]] = []
    for key, value in raw.items():
        if not value:
            continue
        if PLACEHOLDER in key:
            templates.append((_compile(key), value))
        else:
            exact[key] = value
    # A longer template is the more specific one; try it before a shorter
    # template whose literal text is a prefix of it.
    templates.sort(key=lambda item: -len(item[0].pattern))
    return exact, tuple(templates)


def reload_catalog() -> None:
    """Forget the loaded catalogs (tests swap the file; the app never needs this)."""
    _catalog.cache_clear()


def translate_sentence(text: str, lang: str) -> str:
    """The sentence in ``lang``, or unchanged when English, machine-read or unknown."""
    if lang == FALLBACK_LANG or not text or text in MACHINE_READ or is_code(text):
        return text
    exact, templates = _catalog(lang)
    hit = exact.get(text)
    if hit is not None:
        return hit
    for pattern, translated in templates:
        match = pattern.fullmatch(text)
        if match:
            return _fill(translated, match.groups())
    return text


def translate_detail(detail: Any, lang: str) -> Any:
    """A ``detail`` of any shape: a sentence is translated, a dict's ``message`` is, anything else passes through."""
    if isinstance(detail, str):
        return translate_sentence(detail, lang)
    if isinstance(detail, dict) and isinstance(detail.get("message"), str):
        return {**detail, "message": translate_sentence(detail["message"], lang)}
    return detail


def json_error(status_code: int, detail: Any) -> JSONResponse:
    """A hand-built refusal (``return JSONResponse(...)`` instead of ``raise``) that still speaks the system language.

    A few routes answer with a response object because they must *commit* first
    (a stale cover reference is cleaned up before the 404 goes out) or because
    they were written that way; those never reach the exception handler, so
    they translate here instead.
    """
    return JSONResponse(status_code=status_code, content={"detail": translate_detail(detail, current_language())})


async def _handle(request: Request, exc: StarletteHTTPException) -> Response:
    lang = current_language()
    if lang != FALLBACK_LANG:
        translated = translate_detail(exc.detail, lang)
        if translated is not exc.detail:
            exc = StarletteHTTPException(status_code=exc.status_code, detail=translated, headers=exc.headers)
    return await http_exception_handler(request, exc)


def install(app: FastAPI) -> None:
    """Replace FastAPI's default ``HTTPException`` handler with the translating one. Same body, same headers."""
    app.add_exception_handler(StarletteHTTPException, _handle)
