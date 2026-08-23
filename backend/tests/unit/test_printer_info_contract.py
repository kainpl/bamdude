"""PrinterInfo carries name + serial_number and NOTHING else.

The printer model lives in the manager's model cache (``get_model``), not on
``PrinterInfo``. Four call sites wrote ``info.model`` anyway and shipped
AttributeError 500s on configure-slot and every spool→slot assignment path —
kept green by tests whose mocked printer_manager auto-supplies any attribute
(measured live, 2026-08-23). This guard scans the source so a fifth site
fails in CI instead of on a printer.
"""

import pathlib
import re

ALLOWED = ("name", "serial_number")
_ASSIGN = re.compile(r"(\w+)\s*=\s*(?:await\s+)?[\w.]*printer_manager\.get_printer\([^)]*\)")


def test_get_printer_results_use_only_the_fields_printer_info_has():
    offenders = []
    for path in pathlib.Path("backend/app").rglob("*.py"):
        text = path.read_text(encoding="utf-8", errors="replace")
        for m in _ASSIGN.finditer(text):
            var = m.group(1)
            # A bounded window after the assignment — wide enough for the call
            # sites this guards, narrow enough not to trip on var reuse.
            window = text[m.end() : m.end() + 700]
            for attr_m in re.finditer(rf"\b{re.escape(var)}\.(\w+)", window):
                if attr_m.group(1) not in ALLOWED:
                    line = text[: m.end()].count("\n") + 1
                    offenders.append(f"{path}:{line} -> {var}.{attr_m.group(1)}")
    assert not offenders, (
        "PrinterInfo has only name + serial_number; use printer_manager.get_model(printer_id) "
        f"for the model. Offenders: {offenders}"
    )


def test_printer_info_shape_is_pinned():
    from backend.app.services.printer_manager import PrinterInfo

    info = PrinterInfo("n", "s")
    assert (info.name, info.serial_number) == ("n", "s")
    assert not hasattr(info, "model")
