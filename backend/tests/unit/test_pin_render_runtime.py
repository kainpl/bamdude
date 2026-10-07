"""scripts/pin_render_runtime.py: the pinned hashes come only from a SHASUMS256.txt a Node release key signed."""

import importlib.util
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from backend.app.services.render_runtime import ProvisionError

ROOT = Path(__file__).resolve().parents[3]
GPG = shutil.which("gpg")
needs_gpg = pytest.mark.skipif(GPG is None, reason="no gpg on PATH")


def _pin_script():
    spec = importlib.util.spec_from_file_location("pin_render_runtime", ROOT / "scripts" / "pin_render_runtime.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


pin = _pin_script()
BODY = b"0" * 64 + b"  node-v24.17.0-linux-x64.tar.xz\n"


def _signer(name: str) -> tuple[bytes, bytes]:
    """A throwaway key: (its public key, BODY clear-signed with it)."""
    with tempfile.TemporaryDirectory() as tmp:
        home = pin.gpg_home(tmp, GPG)
        base = [GPG, "--homedir", home, "--batch", "--passphrase", "", "--pinentry-mode", "loopback"]
        subprocess.run(
            [*base, "--quick-gen-key", f"{name} <{name}@example.invalid>", "ed25519", "sign", "never"],
            check=True,
            capture_output=True,
        )
        public = subprocess.run([*base, "--armor", "--export"], check=True, capture_output=True).stdout
        signed = subprocess.run([*base, "--clearsign"], input=BODY, check=True, capture_output=True).stdout
    return public, signed


@needs_gpg
def test_a_body_signed_by_a_release_key_is_returned():
    public, signed = _signer("releaser")
    assert pin.verify_shasums(signed, public, GPG).encode() == BODY


@needs_gpg
def test_a_tampered_body_is_refused():
    public, signed = _signer("releaser")
    tampered = signed.replace(b"0" * 64, b"1" * 64)
    with pytest.raises(ProvisionError, match="signature"):
        pin.verify_shasums(tampered, public, GPG)


@needs_gpg
def test_a_body_signed_by_another_key_is_refused():
    release_key, _ = _signer("releaser")
    _, by_stranger = _signer("stranger")
    with pytest.raises(ProvisionError, match="signature"):
        pin.verify_shasums(by_stranger, release_key, GPG)


def test_the_vendored_release_keys_are_there():
    keys = (ROOT / "scripts" / "node-release-keys.asc").read_text(encoding="ascii")
    assert keys.count("-----BEGIN PGP PUBLIC KEY BLOCK-----") >= 20
