"""One 3MF *card* parser — the archive modal, a library file and a product.

A Bambu 3MF carries what the thing *is* — title, designer, licence, description
— in ``3D/3dmodel.model`` metadata, and the designer's supporting material in
``Auxiliaries/``: model pictures, a bill of materials, an assembly guide, plus
BambuStudio's own profile pictures and thumbnails.

This module is ``services/archive.py::ProjectPageParser`` moved out and
generalised. The archive modal read three of the six folders and knew the
archive route's URL shape; products and library files need the other three and
no URLs at all. So the parser now returns a plain :class:`CardData` and
:func:`to_project_page_dict` renders the archive's historic payload from it —
the modal keeps its exact response through the move.

⚠️ **Never write into a library 3MF.** :meth:`ThreeMFCardParser.update_metadata`
is the archive's, and only the archive's: the archive owns its copy of the file,
a library file is the operator's original. Product/library card data lives in
the database.

⚠️ :meth:`ThreeMFCardParser.parse` never raises. The card decorates three
screens; a truncated or non-ZIP file must come back as a ``CardData`` with
``error`` set, not as a 500 on the archive page or an aborted product import.
"""

import html
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

# Folder prefix → category. The first four are what a designer ships and what a
# product imports; the last two are BambuStudio's own and stay separate so the
# archive modal can show them without them turning into product attachments.
CATEGORY_FOLDERS: dict[str, str] = {
    "Auxiliaries/Model Pictures/": "pictures",
    "Auxiliaries/Bill of Materials/": "bom_docs",
    "Auxiliaries/Assembly Guide/": "assembly",
    "Auxiliaries/Others/": "other",
    "Auxiliaries/Profile Pictures/": "profile_pictures",
    "Auxiliaries/.thumbnails/": "thumbnails",
}

# The categories whose members a browser can be asked to RENDER. Everything
# else a designer ships — a bill of materials, an assembly PDF, whatever landed
# in ``Others/`` — is a download, and the routes gate on this: a token surface
# built for ``<img src>`` hands out pictures, never arbitrary documents.
CARD_PICTURE_CATEGORIES = ("pictures", "profile_pictures", "thumbnails")

# 3MF metadata name → CardData attribute.
_FIELD_MAPPING: dict[str, str] = {
    "Title": "title",
    "Description": "description",
    "Designer": "designer",
    "DesignerUserId": "designer_user_id",
    "License": "license",
    "Copyright": "copyright",
    "CreationDate": "creation_date",
    "ModificationDate": "modification_date",
    "Origin": "origin",
    "ProfileTitle": "profile_title",
    "ProfileDescription": "profile_description",
    "ProfileCover": "profile_cover",
    "ProfileUserId": "profile_user_id",
    "ProfileUserName": "profile_user_name",
    "DesignModelId": "design_model_id",
    "DesignProfileId": "design_profile_id",
    "DesignRegion": "design_region",
}

# ``<metadata name="Key">Value</metadata>`` or ``<metadata name="Key" />``.
_METADATA_PATTERN = r'<metadata\s+name="([^"]+)"[^>]*>([^<]*)</metadata>'

# Content types by extension. The image half is the archive modal's original
# table, unchanged; the document half is what the bill-of-materials and
# assembly-guide folders hold, which only became reachable when the parser
# stopped being image-only. Anything unknown stays a download.
#
# ⚠️ No ``svg`` on purpose. An SVG is a document that may carry script, and
# these bytes are served from our own origin — a crafted 3MF would get script
# execution there. Falling through to ``application/octet-stream`` makes it a
# download instead, which is the right answer for a picture nobody can render
# safely.
_CONTENT_TYPES: dict[str, str] = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "jpeg": "image/jpeg",
    "webp": "image/webp",
    "gif": "image/gif",
    "bmp": "image/bmp",
    "pdf": "application/pdf",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
}

_MODEL_PATH = "3D/3dmodel.model"


def content_type_for(name: str) -> str:
    """The content type we would serve this member as.

    Public because the routes need it BEFORE they read anything: which of the two
    card routes can serve a member depends on whether we can name it as an image,
    and the answer has to be the same one :meth:`ThreeMFCardParser.read` will
    give later — a url that promises a picture the serving route then refuses is
    a broken ``<img>`` on the page.
    """
    return _CONTENT_TYPES.get(name.lower().rsplit(".", 1)[-1], "application/octet-stream")


@dataclass
class AuxEntry:
    """One file inside an ``Auxiliaries/`` folder."""

    name: str
    zip_path: str
    size: int


@dataclass
class CardData:
    """What a 3MF says about itself. Every field is optional — most 3MFs fill few."""

    title: str | None = None
    description: str | None = None
    designer: str | None = None
    designer_user_id: str | None = None
    license: str | None = None
    copyright: str | None = None
    creation_date: str | None = None
    modification_date: str | None = None
    origin: str | None = None
    profile_title: str | None = None
    profile_description: str | None = None
    profile_cover: str | None = None
    profile_user_id: str | None = None
    profile_user_name: str | None = None
    design_model_id: str | None = None
    design_profile_id: str | None = None
    design_region: str | None = None

    # category → entries in ZIP order. Every category in ``CATEGORY_FOLDERS`` is
    # always present, empty when the 3MF has no such folder, so callers can index
    # without guarding.
    auxiliaries: dict[str, list[AuxEntry]] = field(
        default_factory=lambda: {category: [] for category in CATEGORY_FOLDERS.values()}
    )
    error: str | None = None


def _unescape(value: str) -> str:
    """Peel every layer of entity encoding and normalise non-breaking spaces.

    BambuStudio has been observed writing triple-encoded payloads
    (``&amp;amp;amp;``), so the loop runs until the string stops changing. The
    ``\\xa0`` normalisation keeps a ``&nbsp;`` from landing in a searchable
    field as a character nobody can type.
    """
    decoded = value.strip()
    previous = None
    while previous != decoded:
        previous = decoded
        decoded = html.unescape(decoded)
    return decoded.replace("\xa0", " ")


class ThreeMFCardParser:
    """Reads the model card out of a 3MF. Read-only except :meth:`update_metadata`."""

    def __init__(self, file_path: Path):
        self.file_path = file_path

    def list_auxiliaries(self) -> dict[str, list[AuxEntry]]:
        """The ``Auxiliaries/`` listing alone — the central directory, nothing else.

        :meth:`parse` decompresses ``3D/3dmodel.model`` and regex-scans it, which
        for a real model is megabytes of work. A route that only needs to answer
        "is this ZIP member one the card offered?" must not pay that, and one
        picture-per-request means it would pay it once per ``<img>`` on the page.

        Same walk :meth:`parse` uses (it calls this), so the two can never
        disagree about what the card offers. Never raises: an unreadable file
        offers nothing, which is the same answer as a file with no auxiliaries —
        callers of this method are asking a membership question, and both answers
        are "no".
        """
        found: dict[str, list[AuxEntry]] = {category: [] for category in CATEGORY_FOLDERS.values()}
        try:
            with zipfile.ZipFile(self.file_path, "r") as zf:
                for name in zf.namelist():
                    for prefix, category in CATEGORY_FOLDERS.items():
                        if not name.startswith(prefix):
                            continue
                        filename = name.split("/")[-1]
                        if filename:  # skip the folder entry itself
                            found[category].append(
                                AuxEntry(name=filename, zip_path=name, size=zf.getinfo(name).file_size)
                            )
                        break
        except Exception:
            pass  # A ZIP we cannot open lists nothing; :meth:`parse` reports the error.
        return found

    def parse(self) -> CardData:
        """Extract metadata and auxiliary listings. Never raises — see module docstring."""
        card = CardData()

        try:
            with zipfile.ZipFile(self.file_path, "r") as zf:
                if _MODEL_PATH in zf.namelist():
                    content = zf.read(_MODEL_PATH).decode("utf-8", errors="ignore")
                    for name, value in re.findall(_METADATA_PATTERN, content):
                        attribute = _FIELD_MAPPING.get(name)
                        if attribute:
                            decoded = _unescape(value)
                            setattr(card, attribute, decoded or None)
        except Exception as e:
            card.error = str(e)

        # Outside the try, and deliberately: a 3MF whose model file is malformed
        # still has pictures worth showing, and the listing has its own guard.
        card.auxiliaries = self.list_auxiliaries()
        return card

    def read(self, zip_path: str) -> tuple[bytes, str] | None:
        """Return ``(bytes, content_type)`` for one member, or ``None`` if absent/unreadable."""
        try:
            with zipfile.ZipFile(self.file_path, "r") as zf:
                if zip_path in zf.namelist():
                    data = zf.read(zip_path)
                    ext = zip_path.lower().split(".")[-1]
                    return (data, _CONTENT_TYPES.get(ext, "application/octet-stream"))
        except Exception:
            pass  # A member that cannot be read is indistinguishable from a missing one.
        return None

    def update_metadata(self, updates: dict) -> bool:
        """Update project page metadata in the 3MF file.

        ⚠️ The ARCHIVE's method. The archive owns its copy of the file; a library
        file is the operator's original and is never written into.

        Args:
            updates: Dict with fields to update (title, description, designer, etc.)

        Returns:
            True if successful, False otherwise.
        """
        try:
            # Read the 3MF file
            with zipfile.ZipFile(self.file_path, "r") as zf_read:
                # Find and read the 3dmodel.model file
                if _MODEL_PATH not in zf_read.namelist():
                    return False

                content = zf_read.read(_MODEL_PATH).decode("utf-8")

                # Update metadata fields
                field_mapping = {
                    "title": "Title",
                    "description": "Description",
                    "designer": "Designer",
                    "license": "License",
                    "copyright": "Copyright",
                    "profile_title": "ProfileTitle",
                    "profile_description": "ProfileDescription",
                }

                for name, xml_name in field_mapping.items():
                    if name in updates and updates[name] is not None:
                        new_value = html.escape(updates[name])
                        # Replace existing metadata or we'd need to add it
                        pattern = rf'(<metadata\s+name="{xml_name}"[^>]*>)[^<]*(</metadata>)'
                        replacement = rf"\g<1>{new_value}\g<2>"
                        content = re.sub(pattern, replacement, content)

                # Write to a temporary file first
                with tempfile.NamedTemporaryFile(delete=False, suffix=".3mf") as tmp:
                    tmp_path = Path(tmp.name)

                # Create new zip with updated content
                with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf_write:
                    for item in zf_read.namelist():
                        if item == _MODEL_PATH:
                            zf_write.writestr(item, content.encode("utf-8"))
                        else:
                            zf_write.writestr(item, zf_read.read(item))

            # Replace original file with updated one
            shutil.move(tmp_path, self.file_path)
            return True

        except Exception:
            # Clean up temp file if it exists
            if "tmp_path" in locals() and tmp_path.exists():
                tmp_path.unlink()
            return False


def to_project_page_dict(card: CardData, archive_id: int) -> dict:
    """Render the archive modal's historic ``project-page`` payload from a card.

    Byte-identical to what ``ProjectPageParser.parse(archive_id)`` returned:
    the 17 metadata keys plus the three image lists, each entry carrying the
    ``/project-image/`` URL the frontend already builds its ``<img>`` from. The
    three product-only categories (``bom_docs``, ``assembly``, ``other``) are
    deliberately NOT in here — the response gained no keys in this move.
    """

    def images(category: str) -> list[dict]:
        return [
            {
                "name": entry.name,
                "path": entry.zip_path,
                "url": f"/api/v1/archives/{archive_id}/project-image/{quote(entry.zip_path, safe='')}",
            }
            for entry in card.auxiliaries[category]
        ]

    payload: dict = {
        "title": card.title,
        "description": card.description,
        "designer": card.designer,
        "designer_user_id": card.designer_user_id,
        "license": card.license,
        "copyright": card.copyright,
        "creation_date": card.creation_date,
        "modification_date": card.modification_date,
        "origin": card.origin,
        "profile_title": card.profile_title,
        "profile_description": card.profile_description,
        "profile_cover": card.profile_cover,
        "profile_user_id": card.profile_user_id,
        "profile_user_name": card.profile_user_name,
        "design_model_id": card.design_model_id,
        "design_profile_id": card.design_profile_id,
        "design_region": card.design_region,
        "model_pictures": images("pictures"),
        "profile_pictures": images("profile_pictures"),
        "thumbnails": images("thumbnails"),
    }
    if card.error is not None:
        payload["_error"] = card.error
    return payload
