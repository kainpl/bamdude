"""HTTP response helpers."""

from urllib.parse import quote


def build_content_disposition(
    filename: str, disposition: str = "attachment", *, ascii_fallback: str | None = None
) -> str:
    """Build an RFC 6266-compliant Content-Disposition header value.

    Starlette/uvicorn encodes response headers as latin-1, so any non-ASCII
    character in a raw ``filename="..."`` parameter raises
    ``UnicodeEncodeError: 'latin-1' codec can't encode characters...``. The
    fix is RFC 5987's ``filename*=UTF-8''<percent-encoded>`` form alongside
    a stripped ASCII fallback in the legacy ``filename="..."`` parameter —
    every modern browser prefers the ``*`` form when present, so the
    original Unicode filename round-trips through Save-As intact.

    ``ascii_fallback`` supplies the legacy ``filename="..."`` parameter when
    dropping the non-ASCII characters would produce a name nobody could use.
    Stripping "Настільна лампа_2026-09-04.zip" leaves "2026-09-04.zip", which
    names the date and not the thing; the product export passes its own slug
    (``product-12_2026-09-04.zip``) instead. The value is sanitised exactly like
    a derived one — it still has to survive latin-1 and a quoted parameter.

    Adapted from upstream `3f58fc74` (issue #1245).
    """
    source = ascii_fallback if ascii_fallback is not None else filename
    fallback = source.encode("ascii", "ignore").decode("ascii").strip(" ._-") or "download"
    fallback = fallback.replace('"', "").replace("\\", "")
    return f"{disposition}; filename=\"{fallback}\"; filename*=UTF-8''{quote(filename)}"
