"""Pin the official Node.js for every platform BamDude ships (spec §6.1).

Reads the release's SHASUMS256.txt.asc from nodejs.org/dist and accepts its
hashes only when one of the Node.js release keys vendored in
scripts/node-release-keys.asc signed it (gpg; TLS alone is not trusted) --
never a third-party build -- then takes each platform's official archive
through render_runtime.artifacts_from_shasums, sizes it with a HEAD request and
rewrites backend/app/data/render_runtime.json. Then it provisions the new pin
into <repo>/runtime and writes the stand's golden hashes on it.

Update policy (spec §6.3): the pin is reviewed at every BamDude release;
between releases a patch release carries a new pin when a Node.js security
bulletin touches V8 or a module the render script uses (zlib). The pin's date
is in the manifest.

The release keys are every fingerprint of nodejs/release-keys keys.list
(vendored from commit 481637f813e9, 2026-09-01). A release signed by a key
that is not there is refused: refresh the file from that repository after
checking who the new releaser is.

    python scripts/pin_render_runtime.py [--version vX.Y.Z]
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from backend.app.services.render_runtime import (  # noqa: E402
    MANIFEST,
    PLATFORMS,
    ProvisionError,
    artifacts_from_shasums,
    locate_in,
    provision,
)

DIST = "https://nodejs.org/dist"
KEYS = Path(__file__).resolve().with_name("node-release-keys.asc")
LINE = 24
HEADERS = {"User-Agent": "BamDude render-runtime pin"}


def _open(url: str, method: str = "GET"):
    return urllib.request.urlopen(urllib.request.Request(url, headers=HEADERS, method=method), timeout=60)


def latest_lts() -> str:
    """The newest Node.js LTS release of the pinned line (index.json lists the newest first)."""
    with _open(f"{DIST}/index.json") as resp:
        releases = json.load(resp)
    for release in releases:
        if release.get("lts") and release["version"].startswith(f"v{LINE}."):
            return release["version"]
    raise SystemExit(f"no Node.js {LINE} LTS release in {DIST}/index.json")


def _size(url: str) -> int:
    with _open(url, "HEAD") as resp:
        length = resp.headers.get("Content-Length")
    if length:
        return int(length)
    size = 0
    with _open(url) as resp:
        while block := resp.read(1024 * 1024):
            size += len(block)
    return size


def gpg_home(path: str, gpg: str) -> str:
    """``path`` as this gpg takes a --homedir: Git for Windows ships an MSYS gpg, which refuses ``C:\\...``."""
    if os.name != "nt":
        return path
    version = subprocess.run([gpg, "--version"], capture_output=True, text=True, check=True).stdout
    home_line = next((line for line in version.splitlines() if line.startswith("Home:")), "")
    if not home_line.split(":", 1)[-1].strip().startswith("/"):
        return path  # a native Windows gpg (Gpg4win) takes the Windows path as it is
    win = PureWindowsPath(path)
    return "/" + win.drive[0].lower() + "/" + "/".join(win.parts[1:])


def verify_shasums(signed: bytes, keys: bytes, gpg: str) -> str:
    """The body of a clear-signed SHASUMS256.txt.asc -- only when one of ``keys`` made a valid signature.

    A throwaway keyring holds nothing but ``keys``; the data goes through stdin (the MSYS gpg reads
    no Windows path) and the verdict is gpg's VALIDSIG status line, not its exit code alone.
    """
    with tempfile.TemporaryDirectory() as tmp:
        base = [gpg, "--homedir", gpg_home(tmp, gpg), "--batch"]
        subprocess.run([*base, "--quiet", "--import"], input=keys, capture_output=True, check=True)
        result = subprocess.run([*base, "--status-fd", "2", "--decrypt"], input=signed, capture_output=True)
    status = result.stderr.decode("utf-8", "replace")
    if result.returncode != 0 or "[GNUPG:] VALIDSIG " not in status:
        raise ProvisionError("SHASUMS256.txt.asc: no valid signature by a vendored Node.js release key")
    return result.stdout.decode("utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--version", default=None, help=f"vX.Y.Z; default: the newest Node.js {LINE} LTS")
    args = parser.parse_args(argv)
    version = args.version or latest_lts()

    gpg = shutil.which("gpg")
    if gpg is None:
        print("gpg is required: the pin is taken only from a signed SHASUMS256.txt.asc", file=sys.stderr)
        return 1
    with _open(f"{DIST}/{version}/SHASUMS256.txt.asc") as resp:
        signed = resp.read()
    try:
        shasums = verify_shasums(signed, KEYS.read_bytes(), gpg)
        artifacts = artifacts_from_shasums(shasums, version)
    except ProvisionError as exc:
        print(exc, file=sys.stderr)
        return 1

    platforms = {}
    for key in PLATFORMS:
        name, sha256 = artifacts[key]
        url = f"{DIST}/{version}/{name}"
        platforms[key] = {"url": url, "sha256": sha256, "bytes": _size(url)}
        print(f"{key}: {platforms[key]['bytes']} bytes {sha256}")

    manifest = {
        "product": "node",
        "source": "nodejs.org/dist",
        "version": version,
        "pinned_at": date.today().isoformat(),
        "platforms": platforms,
    }
    MANIFEST.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"pinned {version} -> {MANIFEST}")

    # The stand's golden hashes belong to the pinned Node: provision it here and write them on it.
    target = provision(ROOT / "runtime", manifest=manifest)
    found = locate_in(ROOT / "runtime")
    if found is None:
        print(f"provisioned {target}, but no Node executable is there", file=sys.stderr)
        return 1
    print(f"provisioned {target}")
    return subprocess.call(
        [
            sys.executable,
            "-m",
            "backend.app.part_render_probe",
            "render",
            "--write-golden",
            "--node",
            str(found.executable),
        ],
        cwd=ROOT,
    )


if __name__ == "__main__":
    sys.exit(main())
