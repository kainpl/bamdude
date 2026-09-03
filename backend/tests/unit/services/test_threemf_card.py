"""One 3MF card parser for archives, library files and products.

``ThreeMFCardParser`` is ``ProjectPageParser`` moved out of ``services/archive``
and generalised: the same 17 metadata keys and the same unescape loop, plus all
six ``Auxiliaries/`` folders instead of the three the archive modal read.

Two things are pinned deliberately:

* ``parse()`` NEVER raises. A truncated 3MF, a file that is not a ZIP at all, a
  path that does not exist — every one of them comes back as a ``CardData`` with
  ``error`` set. The card is decoration on three screens; it must not be able to
  500 an archive page or abort a product import.
* ``to_project_page_dict(card, archive_id)`` reproduces the OLD parser's dict
  byte-for-byte, image urls included. The archive modal keeps working through
  the move, so the expected dict below is a literal copied from the old
  implementation's output rather than something rebuilt from the new one.
"""

import zipfile
from pathlib import Path

import pytest

from backend.app.services.threemf_card import CATEGORY_FOLDERS, ThreeMFCardParser, to_project_page_dict

# Payload bytes are named so the size assertions read as "what we wrote",
# never as a magic number somebody has to re-derive.
PNG_A = b"\x89PNG-a"
JPG_B = b"jpeg-bytes-b"
BOM_CSV = b"part,qty\n"
GUIDE_PDF = b"%PDF-1.4 guide"
NOTES_TXT = b"notes"
PNG_PROFILE = b"\x89PNG-p"
PNG_THUMB = b"\x89PNG-t"

# `&amp;amp;amp;` is BambuStudio's observed triple encoding; `&amp;nbsp;` is the
# non-breaking space the parser normalises to a plain one.
MODEL_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    "<model>"
    '<metadata name="Title">Foo &amp;amp;amp; Bar</metadata>'
    '<metadata name="Designer">Chef&amp;amp;amp;koch</metadata>'
    '<metadata name="DesignerUserId">10086</metadata>'
    '<metadata name="License">CC&amp;amp;amp;BY</metadata>'
    '<metadata name="Description">A&amp;nbsp;model &amp;amp;amp; more</metadata>'
    '<metadata name="Copyright">(c) nobody</metadata>'
    '<metadata name="CreationDate">2026-01-02</metadata>'
    '<metadata name="ModificationDate">2026-01-03</metadata>'
    '<metadata name="Origin">original</metadata>'
    '<metadata name="ProfileTitle">0.20mm Standard</metadata>'
    '<metadata name="ProfileDescription">fast</metadata>'
    '<metadata name="ProfileCover">cover.png</metadata>'
    '<metadata name="ProfileUserId">42</metadata>'
    '<metadata name="ProfileUserName">someone</metadata>'
    '<metadata name="DesignModelId">1234567</metadata>'
    '<metadata name="DesignProfileId">7654321</metadata>'
    '<metadata name="DesignRegion">Global</metadata>'
    '<metadata name="Application">BambuStudio</metadata>'
    "</model>"
)


@pytest.fixture
def card_3mf(tmp_path: Path) -> Path:
    """A 3MF with the full metadata block and one file in each of the six folders."""
    path = tmp_path / "card.3mf"
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("3D/3dmodel.model", MODEL_XML)
        zf.writestr("Auxiliaries/Model Pictures/a.png", PNG_A)
        zf.writestr("Auxiliaries/Model Pictures/b.jpg", JPG_B)
        zf.writestr("Auxiliaries/Bill of Materials/bom.csv", BOM_CSV)
        zf.writestr("Auxiliaries/Assembly Guide/guide.pdf", GUIDE_PDF)
        zf.writestr("Auxiliaries/Others/notes.txt", NOTES_TXT)
        zf.writestr("Auxiliaries/Profile Pictures/p.png", PNG_PROFILE)
        zf.writestr("Auxiliaries/.thumbnails/t.png", PNG_THUMB)
    return path


class TestMetadata:
    def test_every_key_is_mapped(self, card_3mf: Path):
        card = ThreeMFCardParser(card_3mf).parse()

        assert card.error is None
        assert card.designer_user_id == "10086"
        assert card.copyright == "(c) nobody"
        assert card.creation_date == "2026-01-02"
        assert card.modification_date == "2026-01-03"
        assert card.origin == "original"
        assert card.profile_title == "0.20mm Standard"
        assert card.profile_description == "fast"
        assert card.profile_cover == "cover.png"
        assert card.profile_user_id == "42"
        assert card.profile_user_name == "someone"
        assert card.design_model_id == "1234567"
        assert card.design_profile_id == "7654321"
        assert card.design_region == "Global"

    def test_triple_encoded_entities_are_peeled_to_the_plain_string(self, card_3mf: Path):
        card = ThreeMFCardParser(card_3mf).parse()

        assert card.title == "Foo & Bar"
        assert card.designer == "Chef&koch"
        assert card.license == "CC&BY"

    def test_non_breaking_space_becomes_a_plain_space(self, card_3mf: Path):
        """`&nbsp;` unescapes to U+00A0, which then breaks search and wraps oddly."""
        card = ThreeMFCardParser(card_3mf).parse()

        assert card.description == "A model & more"
        assert "\xa0" not in card.description

    def test_a_3mf_without_a_model_part_yields_empty_fields_not_an_error(self, tmp_path: Path):
        path = tmp_path / "bare.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")

        card = ThreeMFCardParser(path).parse()

        assert card.error is None
        assert card.title is None
        assert card.auxiliaries == {category: [] for category in CATEGORY_FOLDERS.values()}


class TestAuxiliaries:
    def test_all_six_folders_are_categorised(self, card_3mf: Path):
        card = ThreeMFCardParser(card_3mf).parse()

        assert set(card.auxiliaries) == {
            "pictures",
            "bom_docs",
            "assembly",
            "other",
            "profile_pictures",
            "thumbnails",
        }
        assert [e.name for e in card.auxiliaries["pictures"]] == ["a.png", "b.jpg"]
        assert [e.name for e in card.auxiliaries["bom_docs"]] == ["bom.csv"]
        assert [e.name for e in card.auxiliaries["assembly"]] == ["guide.pdf"]
        assert [e.name for e in card.auxiliaries["other"]] == ["notes.txt"]
        assert [e.name for e in card.auxiliaries["profile_pictures"]] == ["p.png"]
        assert [e.name for e in card.auxiliaries["thumbnails"]] == ["t.png"]

    def test_entries_carry_the_zip_path_and_the_size(self, card_3mf: Path):
        card = ThreeMFCardParser(card_3mf).parse()

        first, second = card.auxiliaries["pictures"]
        assert first.zip_path == "Auxiliaries/Model Pictures/a.png"
        assert first.size == len(PNG_A)
        assert second.zip_path == "Auxiliaries/Model Pictures/b.jpg"
        assert second.size == len(JPG_B)
        assert card.auxiliaries["assembly"][0].size == len(GUIDE_PDF)

    def test_directory_entries_are_not_listed(self, tmp_path: Path):
        """Some writers store the folder itself; it has no filename to show."""
        path = tmp_path / "dirs.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Auxiliaries/Model Pictures/", b"")
            zf.writestr("Auxiliaries/Model Pictures/a.png", PNG_A)

        card = ThreeMFCardParser(path).parse()

        assert [e.name for e in card.auxiliaries["pictures"]] == ["a.png"]


class TestRead:
    def test_reads_bytes_and_mime(self, card_3mf: Path):
        result = ThreeMFCardParser(card_3mf).read("Auxiliaries/Model Pictures/a.png")

        assert result == (PNG_A, "image/png")

    def test_mime_covers_the_document_categories_too(self, card_3mf: Path):
        parser = ThreeMFCardParser(card_3mf)

        assert parser.read("Auxiliaries/Assembly Guide/guide.pdf") == (GUIDE_PDF, "application/pdf")
        assert parser.read("Auxiliaries/Bill of Materials/bom.csv") == (BOM_CSV, "text/csv")

    def test_svg_stays_a_download(self, tmp_path: Path):
        """An SVG is a scriptable document served from our own origin."""
        path = tmp_path / "svg.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("Auxiliaries/Model Pictures/x.svg", b"<svg/>")

        assert ThreeMFCardParser(path).read("Auxiliaries/Model Pictures/x.svg") == (
            b"<svg/>",
            "application/octet-stream",
        )

    def test_a_missing_entry_is_none(self, card_3mf: Path):
        assert ThreeMFCardParser(card_3mf).read("Auxiliaries/Model Pictures/nope.png") is None

    def test_a_broken_zip_is_none_not_an_exception(self, tmp_path: Path):
        path = tmp_path / "broken.3mf"
        path.write_bytes(b"not a zip at all")

        assert ThreeMFCardParser(path).read("anything") is None


class TestParseNeverRaises:
    def test_a_file_that_is_not_a_zip_sets_error(self, tmp_path: Path):
        path = tmp_path / "broken.3mf"
        path.write_bytes(b"not a zip at all")

        card = ThreeMFCardParser(path).parse()

        assert card.error
        assert card.title is None
        assert card.auxiliaries == {category: [] for category in CATEGORY_FOLDERS.values()}

    def test_a_missing_path_sets_error(self, tmp_path: Path):
        card = ThreeMFCardParser(tmp_path / "gone.3mf").parse()

        assert card.error


class TestProjectPageCompatibility:
    """The archive modal's payload must survive the move unchanged.

    The literal below is the OLD ``ProjectPageParser.parse(7)`` output for the
    fixture, captured before the class was moved.
    """

    def test_dict_matches_the_old_parsers_output(self, card_3mf: Path):
        card = ThreeMFCardParser(card_3mf).parse()

        assert to_project_page_dict(card, 7) == {
            "title": "Foo & Bar",
            "description": "A model & more",
            "designer": "Chef&koch",
            "designer_user_id": "10086",
            "license": "CC&BY",
            "copyright": "(c) nobody",
            "creation_date": "2026-01-02",
            "modification_date": "2026-01-03",
            "origin": "original",
            "profile_title": "0.20mm Standard",
            "profile_description": "fast",
            "profile_cover": "cover.png",
            "profile_user_id": "42",
            "profile_user_name": "someone",
            "design_model_id": "1234567",
            "design_profile_id": "7654321",
            "design_region": "Global",
            "model_pictures": [
                {
                    "name": "a.png",
                    "path": "Auxiliaries/Model Pictures/a.png",
                    "url": "/api/v1/archives/7/project-image/Auxiliaries%2FModel%20Pictures%2Fa.png",
                },
                {
                    "name": "b.jpg",
                    "path": "Auxiliaries/Model Pictures/b.jpg",
                    "url": "/api/v1/archives/7/project-image/Auxiliaries%2FModel%20Pictures%2Fb.jpg",
                },
            ],
            "profile_pictures": [
                {
                    "name": "p.png",
                    "path": "Auxiliaries/Profile Pictures/p.png",
                    "url": "/api/v1/archives/7/project-image/Auxiliaries%2FProfile%20Pictures%2Fp.png",
                }
            ],
            "thumbnails": [
                {
                    "name": "t.png",
                    "path": "Auxiliaries/.thumbnails/t.png",
                    "url": "/api/v1/archives/7/project-image/Auxiliaries%2F.thumbnails%2Ft.png",
                }
            ],
        }

    def test_the_bill_of_materials_folders_stay_out_of_the_archive_payload(self, card_3mf: Path):
        """The archive response gained no keys — only the dataclass did."""
        card = ThreeMFCardParser(card_3mf).parse()
        payload = to_project_page_dict(card, 7)

        assert "bom_docs" not in payload
        assert "assembly" not in payload
        assert "other" not in payload
        assert "auxiliaries" not in payload

    def test_the_error_travels_as_underscore_error(self, tmp_path: Path):
        path = tmp_path / "broken.3mf"
        path.write_bytes(b"not a zip at all")
        card = ThreeMFCardParser(path).parse()

        payload = to_project_page_dict(card, 7)

        assert payload["_error"] == card.error

    def test_a_clean_parse_carries_no_error_key(self, card_3mf: Path):
        card = ThreeMFCardParser(card_3mf).parse()

        assert "_error" not in to_project_page_dict(card, 7)

    def test_the_response_schema_still_accepts_the_payload(self, card_3mf: Path):
        from backend.app.schemas.archive import ProjectPageResponse

        card = ThreeMFCardParser(card_3mf).parse()

        response = ProjectPageResponse(**to_project_page_dict(card, 7))

        assert response.title == "Foo & Bar"
        assert len(response.model_pictures) == 2


class TestUpdateMetadata:
    """Moved verbatim — archive-only, and the ONLY method here that writes.

    A library 3MF is never written into (spec §Risks); this method exists for
    the archive's own copy of the file.
    """

    def test_writes_the_field_back_and_keeps_the_other_members(self, card_3mf: Path):
        parser = ThreeMFCardParser(card_3mf)

        assert parser.update_metadata({"title": "Renamed", "designer": None}) is True

        reparsed = parser.parse()
        assert reparsed.title == "Renamed"
        assert reparsed.designer == "Chef&koch"
        assert [e.name for e in reparsed.auxiliaries["pictures"]] == ["a.png", "b.jpg"]
        assert parser.read("Auxiliaries/Model Pictures/a.png") == (PNG_A, "image/png")

    def test_escapes_on_the_way_in_and_unescapes_on_the_way_out(self, card_3mf: Path):
        parser = ThreeMFCardParser(card_3mf)

        assert parser.update_metadata({"title": "Nuts & Bolts"}) is True

        assert parser.parse().title == "Nuts & Bolts"

    def test_a_3mf_without_a_model_part_is_false_not_an_exception(self, tmp_path: Path):
        path = tmp_path / "bare.3mf"
        with zipfile.ZipFile(path, "w") as zf:
            zf.writestr("[Content_Types].xml", "<Types/>")

        assert ThreeMFCardParser(path).update_metadata({"title": "x"}) is False

    def test_a_broken_zip_is_false_not_an_exception(self, tmp_path: Path):
        path = tmp_path / "broken.3mf"
        path.write_bytes(b"not a zip at all")

        assert ThreeMFCardParser(path).update_metadata({"title": "x"}) is False


class TestTheOldNameStillResolves:
    def test_archive_module_re_exports_the_parser(self):
        """``services.archive.ProjectPageParser`` is an alias for one pass."""
        from backend.app.services.archive import ProjectPageParser

        assert ProjectPageParser is ThreeMFCardParser
