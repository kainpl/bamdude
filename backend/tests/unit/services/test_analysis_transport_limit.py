"""analysis_transport takes a per-caller ceiling; the analysis default does not move (plan E3, task 17)."""

import pytest

from backend.app.services.analysis_transport import ARTIFACT_BYTES, AnalysisArtifact, describe
from backend.app.services.preview_protocol import PreviewError

ATTEMPT = "a" * 32
REF = {"key": f"{ATTEMPT}_partrender", "size": ARTIFACT_BYTES + 1, "digest": "0" * 64}


def test_the_default_ceiling_is_the_analysis_one():
    with pytest.raises(PreviewError):
        AnalysisArtifact.parse(REF, ATTEMPT, "partrender")


def test_a_caller_may_raise_its_own_ceiling():
    assert AnalysisArtifact.parse(REF, ATTEMPT, "partrender", 2 * ARTIFACT_BYTES).size == ARTIFACT_BYTES + 1


def test_describe_honours_the_ceiling(tmp_path):
    path = tmp_path / "x.bin"
    path.write_bytes(b"0" * 64)
    with pytest.raises(PreviewError):
        describe(path, ATTEMPT, 10**30, "partrender", 32)
    assert describe(path, ATTEMPT, 10**30, "partrender", 64).size == 64
