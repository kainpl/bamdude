import io
import json
from pathlib import Path
from types import SimpleNamespace

from backend.app import render_browser_probe_child as child


def _stdin(monkeypatch, cmd: list[str]) -> None:
    monkeypatch.setattr(child.sys, "stdin", io.StringIO(json.dumps({"cmd": cmd}) + "\n"))


def test_the_probe_child_runs_only_the_provisioned_browser(tmp_path: Path, monkeypatch):
    # worker_guardian launches only allowlisted modules; a child that ran any
    # argv it was handed would turn that allowlist into "run anything".
    exe = tmp_path / "chrome-headless-shell"
    exe.write_bytes(b"")
    monkeypatch.setattr(child, "locate", lambda _app_dir: SimpleNamespace(executable=exe))
    calls: list[list[str]] = []
    monkeypatch.setattr(child.subprocess, "call", lambda cmd, **_kw: calls.append(cmd) or 0)

    _stdin(monkeypatch, ["/bin/sh", "-c", "id"])
    assert child.main() == 2
    assert calls == []

    _stdin(monkeypatch, [str(exe), "--headless"])
    assert child.main() == 0
    assert calls == [[str(exe), "--headless"]]


def test_the_probe_child_refuses_without_a_provisioned_browser(monkeypatch):
    monkeypatch.setattr(child, "locate", lambda _app_dir: None)
    monkeypatch.setattr(child.subprocess, "call", lambda *_a, **_kw: 0)
    _stdin(monkeypatch, ["chrome-headless-shell"])
    assert child.main() == 2
