"""Backend internationalization — JSON-file-based translation system.

Translations are stored as JSON files in ``backend/app/data/`` following the
naming convention ``{namespace}_{lang}.json`` (e.g. ``telegram_ui_en.json``).

Usage::

    from backend.app.i18n import t, get_language

    lang = await get_language()
    text = t(lang, "telegram_ui", "start.welcome", name="User")
"""

import json
import logging
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).parent.parent / "data"

# Fallback language when requested language file doesn't exist
FALLBACK_LANG = "en"


@lru_cache(maxsize=32)
def _load_translations(namespace: str, lang: str) -> dict:
    """Load and cache a translation file.

    Returns an empty dict if the file doesn't exist.
    """
    path = DATA_DIR / f"{namespace}_{lang}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed to load translations %s: %s", path, e)
        return {}


def t(lang: str, namespace: str, key: str, **kwargs: Any) -> str:
    """Get a translated string.

    Args:
        lang: Language code (``"en"``, ``"uk"``, etc.).
        namespace: File prefix in ``data/`` (e.g. ``"telegram_ui"``).
        key: Dot-separated key path (e.g. ``"start.welcome"``).
        **kwargs: Values to interpolate via ``str.format()``.

    Returns:
        Translated string, or the raw key if not found.
    """
    translations = _load_translations(namespace, lang)

    # Navigate nested keys
    value: Any = translations
    for part in key.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
            break

    # Fallback to FALLBACK_LANG
    if value is None and lang != FALLBACK_LANG:
        translations = _load_translations(namespace, FALLBACK_LANG)
        value = translations
        for part in key.split("."):
            if isinstance(value, dict):
                value = value.get(part)
            else:
                value = None
                break

    if value is None:
        return key

    if not isinstance(value, str):
        return key

    if kwargs:
        try:
            return value.format(**kwargs)
        except KeyError:
            return value

    return value


def invalidate_cache() -> None:
    """Clear the translation cache (call after language change)."""
    _load_translations.cache_clear()


# MarkdownV2 special characters that must be escaped
_MD_ESCAPE_RE = re.compile(r"([_*\[\]()~`>#+\-=|{}.!\\])")


def escape_md(text: str) -> str:
    """Escape special characters for Telegram MarkdownV2."""
    return _MD_ESCAPE_RE.sub(r"\\\1", str(text))


async def get_language() -> str:
    """Read the current system language from DB settings."""
    try:
        from sqlalchemy import select

        from backend.app.core.database import async_session
        from backend.app.models.settings import Settings

        async with async_session() as db:
            result = await db.execute(select(Settings.value).where(Settings.key == "language"))
            lang = result.scalar_one_or_none()
            return lang or FALLBACK_LANG
    except Exception:
        return FALLBACK_LANG
