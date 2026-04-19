"""Printer model display names mapping.

Maps short model codes (as stored in DB) to user-friendly display names.
"""

# code → display name (used in UI dropdowns, badges etc.)
PRINTER_MODEL_DISPLAY_NAMES: dict[str, str] = {
    "*": "All models",
    "X1C": "X1 Carbon",
    "X1": "X1",
    "X1E": "X1E",
    "P1P": "P1P",
    "P1S": "P1S",
    "P2S": "P2S",
    "A1": "A1",
    "A1 Mini": "A1 Mini",
    "H2D": "H2D",
    "H2D Pro": "H2D Pro",
    "H2C": "H2C",
    "H2S": "H2S",
}


def get_model_display_name(code: str) -> str:
    """Get display name for a printer model code."""
    return PRINTER_MODEL_DISPLAY_NAMES.get(code, code)
