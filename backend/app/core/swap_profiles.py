"""Catalog of swap-mode profiles.

A *swap profile* is a named combination of a printer model + a specific swap
mechanism revision. It selects which set of macros fires when that printer
runs a swap-mode print.

Why a catalog (and not an open string field on :class:`Macro`):
    * Prevents typos — admin picks from a known list.
    * Keeps a human-readable label separate from the identifier used in
      storage and the runtime matcher.
    * Allows filtering the UI by printer model when an admin is editing a
      macro or picking the active profile for a printer.

To add a new variant, add a single entry here (plus the built-in macros for
it in the latest migration's ``seed``). The frontend fetches this list via
``GET /api/v1/macros/swap-profiles``; no enum-in-frontend duplication.
"""

from __future__ import annotations

from typing import Any

# id → { models: list[str], label: str, description: str | None }
#   * ``models`` — printer model codes the profile applies to. The macro
#     editor filters profile options by the models already selected.
#   * ``label`` — shown in dropdowns; keep concise, English-only. Frontend
#     is free to override via i18n if needed.
#   * ``description`` — free-form hint shown under the dropdown.
SWAP_PROFILES: dict[str, dict[str, Any]] = {
    "a1mini_kit": {
        "models": ["A1 Mini"],
        "label": "Kit Edition",
        "description": "A1 Mini swap mechanism — kit-assembled variant.",
    },
    "a1mini_stl": {
        "models": ["A1 Mini"],
        "label": "STL Edition",
        "description": "A1 Mini swap mechanism — self-printed STL variant.",
    },
    "jobox-a1": {
        "models": ["A1"],
        "label": "JobOx A1",
        "description": "JobOx swap mechanism for the full-size A1.",
    },
}


def profile_applies_to_model(profile_id: str, model: str | None) -> bool:
    """Return True if ``profile_id`` lists ``model`` as an applicable model."""
    profile = SWAP_PROFILES.get(profile_id)
    if profile is None or not model:
        return False
    return model in profile["models"]


def list_profiles_for_model(model: str | None) -> list[dict[str, Any]]:
    """Return the catalog entries whose ``models`` list contains ``model``.

    Output is shaped for the frontend dropdown: ``[{"id", "label",
    "description", "models"}, ...]``. Returns an empty list for an unknown
    model, which the UI reads as "no swap variant available".
    """
    if not model:
        return []
    return [{"id": pid, **profile} for pid, profile in SWAP_PROFILES.items() if model in profile["models"]]
