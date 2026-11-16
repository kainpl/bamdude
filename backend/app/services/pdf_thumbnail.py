"""The first page of one PDF as a bounded PNG. Imported ONLY by the disposable preview child (preview_render,
operation ``pdf_png``): this is the one module that loads PDFium, and main never imports it.

A PDF is an untrusted document. The page's declared size is read and refused before anything is rasterised, the
picture has a fixed longer side whatever the page claims, forms are never initialised (so no form field is drawn
and no document script has an environment to run in), and the attempt's time, memory and bytes are bounded by the
parent that owns this process. Every refusal is a closed reason — never the library's text, which can quote the
document.
"""

from __future__ import annotations

import math
from pathlib import Path

from backend.app.services.preview_protocol import (
    PDF_PAGE_POINTS,
    PDF_PNG_BYTES,
    PDF_THUMBNAIL_PIXELS,
    PreviewError,
    remaining,
)

_PASSWORD_ERROR = 4  # FPDF_ERR_PASSWORD


def render_first_page(source: Path, target: Path, deadline: int) -> None:
    """Write ``target`` (a PNG) from the first page of ``source``, or raise a PreviewError that names why not."""
    try:
        import pypdfium2 as pdfium
    except Exception:
        # A broken install (the module, or the library it loads, is not there): reported by the parent, and no
        # picture. Never a reason to fail anything else.
        raise PreviewError("render_failed", why="pdf_renderer_missing") from None

    try:
        document = pdfium.PdfDocument(source)
    except pdfium.PdfiumError as exc:
        encrypted = getattr(exc, "err_code", None) == _PASSWORD_ERROR
        raise PreviewError("render_failed", why="pdf_encrypted" if encrypted else "pdf_unreadable") from None
    try:
        if len(document) < 1:
            raise PreviewError("render_failed", why="pdf_no_pages")
        try:
            page = document[0]
            width, height = page.get_size()
        except pdfium.PdfiumError:
            raise PreviewError("render_failed", why="pdf_unreadable") from None
        try:
            if not (math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0):
                raise PreviewError("render_failed", why="pdf_page_size")
            if max(width, height) > PDF_PAGE_POINTS:
                raise PreviewError("resource_limit", why="pdf_page_size")
            remaining(deadline)
            # The longer side is the fixed target, so the bitmap is at most PDF_THUMBNAIL_PIXELS squared whatever
            # the page declares.
            scale = PDF_THUMBNAIL_PIXELS / max(width, height)
            try:
                bitmap = page.render(scale=scale, may_draw_forms=False)
            except pdfium.PdfiumError:
                raise PreviewError("render_failed", why="pdf_render") from None
            try:
                image = bitmap.to_pil().convert("RGB")
                if max(image.size) > PDF_THUMBNAIL_PIXELS + 1:
                    raise PreviewError("resource_limit", why="pdf_bitmap")
                image.save(target, format="PNG", optimize=True)
            finally:
                bitmap.close()
        finally:
            page.close()
    finally:
        document.close()
    if target.stat().st_size > PDF_PNG_BYTES:
        target.unlink()
        raise PreviewError("resource_limit", why="pdf_png_bytes")
