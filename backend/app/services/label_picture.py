"""A label as a colour picture: the PDF backend's own page, rasterised.

The print dialog's preview and "save as PNG" must show what the driver path
prints — vector type, the colour block, the codes where the PDF puts them. So
the picture is not drawn a third way: the design goes through
``render_template_pdf`` (the one walk, ``label_canvas.draw_template``, over the
PDF backend) and each page is rasterised here at the dots per inch asked for.

⚠️ **Nothing is scaled.** A page is the design's own size in millimetres, the
picture is that size times the resolution, and the PNG records the resolution
(``pHYs``) so the software that prints it keeps the size too.

⚠️ **The one module of main that imports PDFium** (guard:
``test_only_the_childs_module_imports_pdfium``). Library PDFs are untrusted
uploads and are opened only in the disposable preview child; the only document
opened here is the one ``render_template_pdf`` produced a moment earlier, from a
design that already passed ``label_gate``. Nothing else may be handed to it.

The 1-bit raster backend (``label_raster``) is untouched: it is what a thermal
device prints, and what a thermal preview shows.
"""

from __future__ import annotations

import io
import threading
import zipfile

from backend.app.services import label_gate
from backend.app.services.label_renderer import render_template_pdf
from backend.app.services.label_template import (
    MAX_LABEL_PICTURES,
    MAX_PICTURE_SIDE_DOTS,
    LabelTemplateSpec,
    colour_picture_problems,
)

#: The resolutions a saved picture is offered at: the two thermal heads in common
#: use and an office printer.
EXPORT_DPIS = (203, 300, 600)
#: What a preview on a screen is drawn at — 8 dots per millimetre.
SCREEN_DPI = 203
#: Labels in one export — the bound itself lives with the other label bounds.
MAX_PICTURES = MAX_LABEL_PICTURES
#: Everything one export answers with, pictures added up. The archive is built in
#: memory, so this is also what one export may hold of it.
MAX_BUNDLE_BYTES = 64 * 1024 * 1024

_NAME_STEM = 60
_ZIP_MOMENT = (1980, 1, 1, 0, 0, 0)

#: PDFium is not thread-safe, and every picture is drawn in a worker thread.
_pdfium = threading.Lock()


class BundleTooLarge(Exception):
    """The pictures of one export add up to more than :data:`MAX_BUNDLE_BYTES`."""


def picture_size(spec: LabelTemplateSpec, dpi: float) -> tuple[int, int]:
    """The picture's size in dots: the design's millimetres at this resolution."""
    return max(1, round(spec.width_mm * dpi / 25.4)), max(1, round(spec.height_mm * dpi / 25.4))


def checked(spec: LabelTemplateSpec, dpi: float, *, labels: int) -> None:
    """Refuse, in one translated sentence, a picture or a batch nobody should draw."""
    label_gate.refuse(colour_picture_problems(spec, dpi, labels=labels))


async def label_pictures(
    spec: LabelTemplateSpec, contexts: list[dict[str, str]], *, dpi: float
) -> tuple[list[bytes], list[str]]:
    """One PNG per context, in order, and what the walk warned about.

    The door every route uses: the bounds are asked first, and the drawing runs
    in a worker thread.
    """
    checked(spec, dpi, labels=len(contexts))
    return await label_gate.draw(render_pictures, spec, contexts, dpi=dpi)


def render_pictures(
    spec: LabelTemplateSpec, contexts: list[dict[str, str]], *, dpi: float
) -> tuple[list[bytes], list[str]]:
    """Synchronous and slow — reach it through :func:`label_pictures`."""
    size = picture_size(spec, dpi)
    if max(size) > MAX_PICTURE_SIDE_DOTS:
        raise ValueError(f"a {size[0]} × {size[1]} dot picture is past the {MAX_PICTURE_SIDE_DOTS} dot bound")

    pdf, warnings = render_template_pdf(spec, contexts)

    import pypdfium2 as pdfium

    pictures: list[bytes] = []
    total = 0
    # ⚠️ The lock is taken per page, not per batch: a preview asked for while two
    # hundred labels are being saved waits for one page, not for all of them.
    with _pdfium:
        document = pdfium.PdfDocument(pdf)
    try:
        for index in range(len(contexts)):
            with _pdfium:
                page = document[index]
                try:
                    bitmap = page.render(scale=dpi / 72, may_draw_forms=False)
                    try:
                        drawn = bitmap.to_pil().convert("RGB")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
            pictures.append(_png(drawn, size, dpi))
            total += len(pictures[-1])
            if total > MAX_BUNDLE_BYTES:
                # Stopped here, not after the last label: the pictures are held in memory.
                raise BundleTooLarge
    finally:
        with _pdfium:
            document.close()
    return pictures, warnings


def _png(drawn, size: tuple[int, int], dpi: float) -> bytes:
    """The page as a PNG of exactly ``size``.

    ⚠️ PDFium rounds a page UP to whole dots; the size promised is the nearest
    whole dot. The difference is at most the last row or column, and it is cut
    or padded with paper — never resampled.
    """
    from PIL import Image

    if drawn.size != size:
        sheet = Image.new("RGB", size, "white")
        sheet.paste(drawn.crop((0, 0, min(drawn.width, size[0]), min(drawn.height, size[1]))), (0, 0))
        drawn = sheet
    out = io.BytesIO()
    drawn.save(out, format="PNG", dpi=(dpi, dpi))
    return out.getvalue()


def picture_names(labels: list[tuple[int, str]]) -> list[str]:
    """A file name per ``(spool number, name)``: the same batch always gets the same names.

    The number leads so a folder sorts like the shelf; the name is what a person
    reads. A label asked for twice is numbered rather than overwritten.
    """
    taken: set[str] = set()
    names: list[str] = []
    for number, name in labels:
        folded = "".join(ch if ch.isalnum() else "-" for ch in (name or "").lower())
        stem = "-".join(part for part in folded.split("-") if part)[:_NAME_STEM].strip("-")
        base = f"{number:04d}-{stem}" if stem else f"{number:04d}"
        candidate, copy = base, 1
        while candidate in taken:
            copy += 1
            candidate = f"{base}-{copy}"
        taken.add(candidate)
        names.append(f"{candidate}.png")
    return names


def bundle(names: list[str], pictures: list[bytes]) -> bytes:
    """The pictures as one archive. Stored, not deflated — a PNG is already compressed.

    Every entry carries the same fixed moment, so the same pictures give the
    same bytes.
    """
    if sum(len(picture) for picture in pictures) > MAX_BUNDLE_BYTES:
        raise BundleTooLarge
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_STORED) as archive:
        for name, picture in zip(names, pictures, strict=True):
            archive.writestr(zipfile.ZipInfo(name, date_time=_ZIP_MOMENT), picture)
    return out.getvalue()


__all__ = [
    "EXPORT_DPIS",
    "MAX_BUNDLE_BYTES",
    "MAX_PICTURES",
    "SCREEN_DPI",
    "BundleTooLarge",
    "bundle",
    "checked",
    "label_pictures",
    "picture_names",
    "picture_size",
    "render_pictures",
]
