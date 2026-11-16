"""No deletion on a printer goes out without a ``may_run`` (G10 B).

The file a job prints from stays only if every door asks at the command. A new
call site that forgets the gate deletes by its place in the file channel alone —
which is exactly what unit B removed. This scans the tree instead of trusting
each site to remember.
"""

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "app"

#: Functions that delete, move or overwrite a file on the printer.
DESTRUCTIVE = {"delete_file_async", "rename_file_async", "clear_sdcard_async"}

#: Where a bare call is the definition's own plumbing, not a door.
PLUMBING = {
    "services/bambu_ftp.py",  # the wrappers themselves
    "services/printer_files/ftp.py",  # passes the caller's may_run through
}


def _calls(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else None
            if name:
                yield name, node


def _has_keyword(call: ast.Call, *names: str) -> bool:
    return any(keyword.arg in names for keyword in call.keywords)


def test_every_destructive_ftp_call_passes_a_may_run():
    missing = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP).as_posix()
        if rel in PLUMBING:
            continue
        for name, call in _calls(ast.parse(path.read_text(encoding="utf-8"))):
            if name in DESTRUCTIVE and not _has_keyword(call, "may_run"):
                missing.append(f"{rel}:{call.lineno} {name}")
    assert not missing, "printer deletions without a may_run gate:\n" + "\n".join(missing)


def test_every_transport_delete_passes_a_may_run():
    """``transport.delete(path)`` / ``internal.delete(path)`` on a printer file transport."""
    missing = []
    for rel in ("api/routes/printers.py", "services/timelapse_files.py", "services/background_dispatch.py", "main.py"):
        tree = ast.parse((APP / rel).read_text(encoding="utf-8"))
        for name, call in _calls(tree):
            if name != "delete" or not isinstance(call.func, ast.Attribute):
                continue
            owner = ast.unparse(call.func.value)
            if "transport" not in owner and owner != "internal":
                continue
            # ``**({"may_run": check} if check is not None else {})`` is the lookup helper's own form.
            starred = any(keyword.arg is None and "may_run" in ast.unparse(keyword.value) for keyword in call.keywords)
            if not (_has_keyword(call, "may_run") or starred):
                missing.append(f"{rel}:{call.lineno} {owner}.delete")
    assert not missing, "transport deletions without a may_run gate:\n" + "\n".join(missing)


def _function(tree: ast.AST, name: str) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found")


def test_the_post_print_cleanup_has_a_cooperative_deadline():
    """M09 / OD-8: no ``wait_for`` that walks away from a step in flight, and every step asks the budget."""
    tree = ast.parse((APP / "main.py").read_text(encoding="utf-8"))
    runner = _function(tree, "_post_print_printer_cleanup")
    assert not [1 for name, _ in _calls(runner) if name == "wait_for"], "the cleanup is cancelled by a timeout again"
    assert "cleanup_deadline" in ast.unparse(runner)

    body = _function(tree, "_cleanup_printer_after_print")
    text = ast.unparse(body)
    # The run-ownership check alone does not know about the budget or the active file.
    assert "_owns_printer_effect" not in text
    for name, call in _calls(body):
        if name in DESTRUCTIVE | {"upload_bytes_async"}:
            gate = next(ast.unparse(k.value) for k in call.keywords if k.arg == "may_run")
            assert gate.startswith("_cleanup_gate("), f"main.py:{call.lineno} {name} is gated by {gate}"


def test_the_dispatcher_deletes_only_through_its_file_gate():
    tree = ast.parse((APP / "services/background_dispatch.py").read_text(encoding="utf-8"))
    for runner in ("_run_reprint_archive", "_run_print_library_file", "_resolve_rack"):
        for name, call in _calls(_function(tree, runner)):
            if name in {"delete_file_async", "_cleanup_sd_card_file"}:
                gate = next(ast.unparse(k.value) for k in call.keywords if k.arg == "may_run")
                assert "_file_gate(job)" in gate, f"{runner}:{call.lineno} {name} is gated by {gate}"
            if name == "_clear_remote_name":
                gate = next(ast.unparse(k.value) for k in call.keywords if k.arg == "gate")
                assert gate == "self._file_gate(job)", f"{runner}:{call.lineno}"
