"""The rules ``services/product_files`` owns, pinned where they can be pinned.

Two of them are values other layers read at call time, and one is a closed set
the FRONTEND switches on. None of the three is enforceable at the reader:
``ProductAttachmentOut.source`` is deliberately a plain ``str`` so a hand-edited
JSON column or a restored backup renders the product page instead of 500-ing it.
The set is therefore enforced HERE, at the writers — which is the only place a
rejection can do something useful.
"""

import re
from pathlib import Path

import pytest

from backend.app.services.product_files import (
    MAX_ATTACHMENT_BYTES,
    MAX_IMPORT_BYTES,
    MAX_IMPORT_MEMBER_BYTES,
    SOURCE_3MF,
    SOURCE_IMPORT,
    SOURCE_MANUAL,
    SOURCE_VALUES,
    attachment_limit,
    exceeds_attachment_limit,
    import_limit,
    import_member_limit,
)

pytestmark = pytest.mark.unit

# The three modules that build an attachment entry. m158 writes ``manual`` too
# and is deliberately absent: migrations are frozen, so it cannot be made to
# import these constants and cannot drift either.
_WRITERS = (
    Path("backend/app/api/routes/products.py"),
    Path("backend/app/services/product_card.py"),
)


def test_the_source_set_is_the_three_the_frontend_knows():
    assert SOURCE_VALUES == ("manual", "3mf", "import")
    assert (SOURCE_MANUAL, SOURCE_3MF, SOURCE_IMPORT) == SOURCE_VALUES


@pytest.mark.parametrize("path", _WRITERS, ids=lambda p: p.name)
def test_no_writer_spells_a_source_of_its_own(path: Path):
    """A literal is how a fourth value gets in.

    Every writer assigns one of the constants above, so a bare ``"source": "…"``
    in these files is either a value nobody added to the set or a copy of one
    that will not move when the set does. The upload route, the 3MF fill and the
    ZIP import are the three; there is no fourth.
    """
    text = path.read_text(encoding="utf-8")
    literals = re.findall(r'"source":\s*"([^"]*)"', text)
    assert literals == [], f"{path} writes a literal source: {literals}"
    assert re.findall(r'"source":\s*(SOURCE_[A-Z0-9_]+)', text), f"{path} builds no attachment entry any more"


def test_every_source_a_writer_uses_is_in_the_set():
    used = set()
    for path in _WRITERS:
        used.update(re.findall(r'"source":\s*(SOURCE_[A-Z0-9_]+)', path.read_text(encoding="utf-8")))
    from backend.app.services import product_files

    for name in used:
        assert getattr(product_files, name) in SOURCE_VALUES, f"{name} is not one of SOURCE_VALUES"


def test_the_limits_are_read_at_call_time(monkeypatch):
    """A module that binds the constant at import keeps the value it saw, so the
    number a 413 REPORTS drifts from the number the gate ENFORCED. It is also
    what lets these tests lower a ceiling instead of producing two gigabytes."""
    assert attachment_limit() == MAX_ATTACHMENT_BYTES
    assert import_limit() == MAX_IMPORT_BYTES
    assert import_member_limit() == MAX_IMPORT_MEMBER_BYTES

    monkeypatch.setattr("backend.app.services.product_files.MAX_ATTACHMENT_BYTES", 5)
    monkeypatch.setattr("backend.app.services.product_files.MAX_IMPORT_BYTES", 7)
    monkeypatch.setattr("backend.app.services.product_files.MAX_IMPORT_MEMBER_BYTES", 6)
    assert (attachment_limit(), import_limit(), import_member_limit()) == (5, 7, 6)
    assert exceeds_attachment_limit(6) is True
    assert exceeds_attachment_limit(5) is False
    assert exceeds_attachment_limit(None) is False


def test_a_member_ceiling_below_the_archive_ceiling():
    """The archive bound is a transport number; the member bound is a memory one,
    because ``store_library_upload`` takes ``bytes``. Inverting them would make
    the memory bound unreachable."""
    assert MAX_IMPORT_MEMBER_BYTES < MAX_IMPORT_BYTES
