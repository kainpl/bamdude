"""HTTP response helpers."""

from urllib.parse import quote


def build_content_disposition(filename: str, disposition: str = "attachment") -> str:
    """Build an RFC 6266-compliant Content-Disposition header value.

    Starlette/uvicorn encodes response headers as latin-1, so any non-ASCII
    character in a raw ``filename="..."`` parameter raises
    ``UnicodeEncodeError: 'latin-1' codec can't encode characters...``. The
    fix is RFC 5987's ``filename*=UTF-8''<percent-encoded>`` form alongside
    a stripped ASCII fallback in the legacy ``filename="..."`` parameter —
    every modern browser prefers the ``*`` form when present, so the
    original Unicode filename round-trips through Save-As intact.

    Adapted from upstream `3f58fc74` (issue #1245).
    """
    ascii_fallback = filename.encode("ascii", "ignore").decode("ascii").strip(" ._-") or "download"
    ascii_fallback = ascii_fallback.replace('"', "").replace("\\", "")
    return f"{disposition}; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"
