"""The Windows release gate's own check of the installer it publishes (spec stage B R6). Standard library only.

``describe`` (the build job): the .exe's sha256, the status of the STEP-preview bundle's receipt in the staged
payload and the install directory limit compiled into the installer, into payload.json.

``check`` (the release job): the .exe about to be published is the one that was built AND the one the full
acceptance installed (one sha256 for all three), both the staged and the INSTALLED receipt name the release pin,
and the acceptance was the full one with every scenario passed. Anything else exits 1, naming why.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def describe(exe: Path, app: Path, version_iss: Path) -> dict:
    receipts = sorted(app.glob("runtime/step-preview/*/*/receipt.json"))
    if len(receipts) != 1:
        raise SystemExit(f"describe: expected one STEP-preview receipt in the payload, found {len(receipts)}")
    status = json.loads(receipts[0].read_text(encoding="utf-8")).get("status")
    limit = re.search(r"#define StepMaxAppDirLen (\d+)", version_iss.read_text(encoding="utf-8"))
    if limit is None:
        raise SystemExit("describe: version.iss names no StepMaxAppDirLen")
    return {
        "sha256": sha256(exe),
        "size_bytes": exe.stat().st_size,
        "receipt_status": status,
        "app_dir_limit": int(limit.group(1)),
    }


def problems(exe: Path, payload: dict, acceptance: dict) -> list[str]:
    found = []
    built = sha256(exe)
    if payload.get("sha256") != built:
        found.append("the installer is not the one the build described")
    if acceptance.get("sha256") != built:
        found.append("the installer is not the one the acceptance installed")
    if payload.get("receipt_status") != "release":
        found.append(f"the payload carries a {payload.get('receipt_status')!r} STEP-preview pin, not 'release'")
    if acceptance.get("installed_receipt_status") != "release":
        found.append(f"the installed bundle says {acceptance.get('installed_receipt_status')!r}, not 'release'")
    if acceptance.get("full") is not True:
        found.append("the acceptance was not the full one")
    failed = [name for name, outcome in (acceptance.get("scenarios") or {}).items() if outcome != "passed"]
    if failed or not acceptance.get("scenarios"):
        found.append(f"scenarios not passed: {', '.join(failed) or 'none recorded'}")
    return found


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check_release_payload.py")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("describe")
    d.add_argument("--exe", type=Path, required=True)
    d.add_argument("--app", type=Path, required=True)
    d.add_argument("--version-iss", type=Path, required=True)
    d.add_argument("--out", type=Path, required=True)
    c = sub.add_parser("check")
    c.add_argument("--exe", type=Path, required=True)
    c.add_argument("--payload", type=Path, required=True)
    c.add_argument("--acceptance", type=Path, required=True)
    a = ap.parse_args(argv)
    if a.cmd == "describe":
        answer = describe(a.exe, a.app, a.version_iss)
        a.out.write_text(json.dumps(answer, indent=1), encoding="utf-8")
        print(json.dumps(answer))
        return 0
    payload = json.loads(a.payload.read_text(encoding="utf-8"))
    acceptance = json.loads(a.acceptance.read_text(encoding="utf-8"))
    found = problems(a.exe, payload, acceptance)
    for line in found:
        print(f"release refused: {line}", file=sys.stderr)
    if not found:
        print("release payload checked: the accepted installer, the release pin")
    return 1 if found else 0


if __name__ == "__main__":
    raise SystemExit(main())
