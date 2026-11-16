"""``X-Label-Warnings`` survives a warning no HTTP header can carry as typed.

A header value is Latin-1 on the wire. A warning quotes what would not encode —
a barcode whose payload is a Cyrillic spool name — so the sentence itself is not
Latin-1, and writing it into the header as typed failed the whole response: the
label that should have arrived with a warning arrived as a 500.

The value is now percent-encoded UTF-8 wherever a character is not printable
ASCII (and for ``%`` itself), so an all-ASCII warning reads as it always did and
``decodeURIComponent`` gives back any other.
"""

from __future__ import annotations

from urllib.parse import unquote

import pytest
from httpx import AsyncClient

from backend.app.models.label_template import LabelTemplate
from backend.app.models.spool import Spool
from backend.app.services import label_gate

pytestmark = [pytest.mark.asyncio, pytest.mark.integration]

NAME = "ПЛА Червоний"  # a Cyrillic spool name


@pytest.fixture
async def spool(db_session) -> Spool:
    row = Spool(material="PLA", subtype="Basic", brand="Polymaker", color_name="Red", rgba="FF0000FF")
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


@pytest.fixture
async def design(db_session) -> LabelTemplate:
    """A barcode fed the spool's name: Code 39 has no Cyrillic, so the walk warns."""
    row = LabelTemplate(
        name="Shelf tag",
        width_mm=60.0,
        height_mm=30.0,
        shape="rect",
        target="driver",
        elements=[
            {
                "type": "barcode",
                "symbology": "code39",
                "x_mm": 2,
                "y_mm": 2,
                "w_mm": 50,
                "h_mm": 12,
                "content": "{display_name}",
            },
        ],
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _body(spool: Spool, design: LabelTemplate) -> dict:
    return {"spools": [{"id": spool.id, "display_name": NAME}], "template_id": design.id}


class TestTheHeaderValue:
    def test_a_plain_warning_reads_as_it_always_did(self):
        headers = label_gate.warning_headers(["element 1 (text) extends past the label edge and will be clipped"])
        assert headers["X-Label-Warnings"] == "element 1 (text) extends past the label edge and will be clipped"
        assert headers["Access-Control-Expose-Headers"] == "X-Label-Warnings"

    def test_several_are_joined_as_before(self):
        assert label_gate.warning_headers(["one", "two"])["X-Label-Warnings"] == "one | two"

    def test_no_warning_no_header(self):
        assert label_gate.warning_headers([]) == {}

    def test_what_a_header_cannot_carry_is_percent_encoded_utf8(self):
        warning = f"element 1 (barcode, code39): cannot encode '{NAME}' — 100%"
        value = label_gate.warning_headers([warning, "two"])["X-Label-Warnings"]
        value.encode("ascii")  # nothing a header would refuse
        assert all(0x20 <= ord(ch) < 0x7F for ch in value)
        assert unquote(value) == f"{warning} | two"
        assert "100%25" in value, "a literal percent sign is encoded, or decoding would misread it"

    def test_a_line_break_cannot_start_another_header(self):
        value = label_gate.warning_headers(["one\r\nX-Injected: 1"])["X-Label-Warnings"]
        assert "\r" not in value and "\n" not in value
        assert unquote(value) == "one\r\nX-Injected: 1"


async def test_the_pdf_arrives_with_a_cyrillic_warning(async_client: AsyncClient, spool, design):
    printed = await async_client.post("/api/v1/inventory/labels", json=_body(spool, design))

    assert printed.status_code == 200, printed.text
    assert printed.content.startswith(b"%PDF")
    warning = unquote(printed.headers["X-Label-Warnings"])
    assert "barcode" in warning
    assert NAME[0] in warning, "the test's premise: the warning quotes the character that would not encode"


async def test_the_picture_arrives_with_a_cyrillic_warning(async_client: AsyncClient, spool, design):
    saved = await async_client.post("/api/v1/inventory/labels/png", json={**_body(spool, design), "dpi": 203})

    assert saved.status_code == 200, saved.text
    assert saved.headers["content-type"] == "image/png"
    assert NAME[0] in unquote(saved.headers["X-Label-Warnings"])
