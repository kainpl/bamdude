"""The vector generator VERIFIES by default — the regeneration habit-path is closed.

T2 review, Important 1: after the Task 2 repoint the generator sourced its
values from the ENGINE, so a habitual ``python -m …generate_vectors`` + commit
would have replaced the frozen shipped-truth with the thing under test — every
golden replay silently becoming engine-vs-engine. The fix: the default mode
recomputes and DIFFS against the committed ``rate_vectors.json``, exiting
non-zero on divergence and writing nothing; overwriting demands the explicit
``--write`` flag. These tests pin both exits.
"""

from __future__ import annotations

import json

from backend.tests.forecast_vectors import generate_vectors as generator


def test_default_mode_verifies_the_pristine_fixture_and_exits_zero(capsys):
    assert generator.main([]) == 0
    assert "verified" in capsys.readouterr().out


def test_default_mode_rejects_a_tampered_fixture_without_touching_it(tmp_path, monkeypatch, capsys):
    """A divergence must come back as a non-zero exit naming the frozen-truth
    rule — and the file must be exactly as tampered afterwards: verify mode
    never writes."""
    document = json.loads(generator.VECTORS_PATH.read_text(encoding="utf-8"))
    document["history"][0]["expected"]["rate"] += 1.0  # a subtle engine "drift"
    tampered = tmp_path / "rate_vectors.json"
    tampered_text = json.dumps(document, indent=2) + "\n"
    tampered.write_text(tampered_text, encoding="utf-8")
    monkeypatch.setattr(generator, "VECTORS_PATH", tampered)

    assert generator.main([]) != 0

    err = capsys.readouterr().err
    assert "MISMATCH" in err
    assert "never the vector" in err, "the failure message must name the frozen-truth rule"
    assert tampered.read_text(encoding="utf-8") == tampered_text, "verify mode must not write"
