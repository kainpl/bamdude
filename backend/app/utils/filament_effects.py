"""How a spool is DRAWN: the swatch effects the frontend knows (``FilamentEffect``).

``effect_type`` is a rendering hint kept apart from ``subtype`` — Bambu's
categorical label — so a user can change how a roll is drawn without touching
what it is. The list mirrors ``frontend/src/components/filamentSwatchHelpers.ts``
and a test pins the two together: a value the form cannot show is not one the
server may write.
"""

from __future__ import annotations

EFFECT_TYPES = frozenset(
    {
        "sparkle",
        "wood",
        "marble",
        "glow",
        "matte",
        "silk",
        "galaxy",
        "rainbow",
        "metal",
        "translucent",
        "gradient",
        "dual-color",
        "tri-color",
        "multicolor",
    }
)


def normalize_effect(value: str | None) -> str | None:
    """A known effect in its canonical spelling, or None ("Dual Color" → "dual-color")."""
    if not value:
        return None
    canonical = value.strip().lower().replace("_", "-").replace(" ", "-")
    return canonical if canonical in EFFECT_TYPES else None


def effect_from_subtype(subtype: str | None) -> str | None:
    """The effect a subtype names, if it names one (upstream b38022ec).

    The two vocabularies line up where a subtype describes a finish — Wood,
    Silk, Sparkle, and the Gradient / Dual Color / Tri Color the colour codes
    upgrade a subtype to. "Silk+" is the Silk finish with a plus on the product
    name. A subtype that names no effect — Basic, Tough, CF — gives None rather
    than an invented overlay.
    """
    if not subtype:
        return None
    return normalize_effect(subtype) or normalize_effect(subtype.strip().rstrip("+"))
