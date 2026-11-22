"""Keep spreadsheet data separate from formula syntax at export boundaries."""


def needs_csv_text_prefix(value: str) -> bool:
    """Identify text that may be parsed as a formula, even after leading whitespace."""
    return value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@"))


def csv_literal(value: object) -> object:
    """Protect text cells while leaving typed numbers and empty cells to csv.writer."""
    if isinstance(value, str) and needs_csv_text_prefix(value):
        return "'" + value
    return value
