"""Tiny PDFs written by hand, byte for byte, for the PDF thumbnail tests. Nothing is downloaded."""

from __future__ import annotations


def _assemble(objects: list[bytes], trailer_extra: bytes = b"") -> bytes:
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\n" % (len(objects) + 1, trailer_extra)
    out += b"startxref\n%d\n%%%%EOF\n" % xref
    return bytes(out)


def page_pdf(width: float = 200, height: float = 300) -> bytes:
    """One page of ``width`` x ``height`` points with a black rectangle over its lower-left quarter."""
    stream = b"0 0 0 rg 0 0 %g %g re f" % (width / 2, height / 2)
    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %g %g] /Contents 4 0 R >>" % (width, height),
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        ]
    )


def empty_pdf() -> bytes:
    """A valid document with no page at all."""
    return _assemble([b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [] /Count 0 >>"])


def encrypted_pdf() -> bytes:
    """A document that names the standard security handler with keys no empty password opens."""
    stream = b"0 0 0 rg 0 0 10 10 re f"
    owner, user = b"A" * 32, b"B" * 32
    return _assemble(
        [
            b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 300] /Contents 4 0 R >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Filter /Standard /V 1 /R 2 /O (" + owner + b") /U (" + user + b") /P -4 >>",
        ],
        trailer_extra=b" /Encrypt 5 0 R /ID [<00112233445566778899aabbccddeeff> <00112233445566778899aabbccddeeff>]",
    )


BROKEN_PDF = b"%PDF-1.4\nthis is not a document\n%%EOF\n"
