"""Nobody reads ``is_dir`` off an FTP listing entry.

``bambu_ftp.list_files`` emits ``is_directory`` (pinned by
``test_bambu_ftp.py::…is_directory…``). Three separate cleanup loops — two in
``background_dispatch`` and one in ``main`` — asked the dict for ``is_dir``
instead, so their "skip directories" guard was always false and a directory in
``/cache`` was handed to DELE, which cannot delete one.

The mistake is invisible: the guard silently never fires, and the delete that
follows fails into an ``except: pass``. Nothing logs, nothing breaks loudly,
and reading the code tells you the opposite of what runs. That is what earns a
drift guard rather than a comment.

⚠️ This checks ``.get("is_dir")`` specifically — reading the key off a dict.
``Path.is_dir()`` is a different thing entirely and is used all over the
codebase quite correctly.
"""

from __future__ import annotations

import re
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[2] / "app"

# ``.get("is_dir")`` / ``.get('is_dir')``, with or without a default.
_WRONG_KEY = re.compile(r"""\.get\(\s*["']is_dir["']""")


def test_no_source_file_reads_the_is_dir_key():
    offenders: list[str] = []
    for source in _BACKEND.rglob("*.py"):
        text = source.read_text(encoding="utf-8")
        for number, line in enumerate(text.splitlines(), start=1):
            if _WRONG_KEY.search(line):
                offenders.append(f"{source.relative_to(_BACKEND.parent.parent)}:{number}")

    assert not offenders, (
        "FTP listing entries carry 'is_directory', never 'is_dir' — these read a key "
        "that is never present, so their guard never fires: " + ", ".join(offenders)
    )


def test_the_listing_producer_still_emits_is_directory():
    """The guard above is only meaningful while this stays true."""
    source = (_BACKEND / "services" / "bambu_ftp.py").read_text(encoding="utf-8")
    assert '"is_directory": is_dir,' in source
