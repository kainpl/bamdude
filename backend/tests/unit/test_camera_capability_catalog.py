"""Camera diagnostics reuse the mirrored Bambu Studio capability catalog."""

from backend.app.utils.printer_configs import camera_capability_catalog


def test_p2s_camera_catalog_preserves_the_bambu_studio_declaration():
    result = camera_capability_catalog("P2S")

    assert result["resolution_supported"] == ["1080p"]
    assert result["virtual_camera"] == "enabled"
    assert result["liveview_remote"] == "tutk"


def test_unknown_model_has_no_invented_camera_capability():
    assert camera_capability_catalog("not-a-model") == {}
