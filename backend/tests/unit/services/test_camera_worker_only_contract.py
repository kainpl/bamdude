"""Main may relay camera frames, but may never own a physical camera reader."""

from pathlib import Path

from backend.app.api.routes import camera as route
from backend.app.services import camera_runtime


def test_camera_routes_have_no_inline_capture_or_ffmpeg_janitor():
    assert not hasattr(route, "generate_rtsp_mjpeg_stream")
    assert not hasattr(route, "generate_chamber_mjpeg_stream")
    assert not hasattr(route, "cleanup_orphaned_streams")
    assert not hasattr(route, "_scan_bambu_ffmpeg_pids")
    assert not hasattr(route, "_active_streams")
    assert not hasattr(route, "_active_chamber_streams")
    assert not hasattr(route, "_spawned_ffmpeg_pids")


def test_camera_runtime_has_no_inline_owner_or_mode_switch():
    assert not hasattr(camera_runtime, "InlineCameraRuntime")
    source = Path(camera_runtime.__file__).read_text(encoding="utf-8")
    assert 'os.environ.get("CAMERA_RUNTIME")' in source  # obsolete-variable warning only
    assert 'configure_camera_runtime("inline")' not in source


def test_main_has_no_ffmpeg_process_scanner():
    main_source = Path(__file__).parents[3].joinpath("app", "main.py").read_text(encoding="utf-8")
    assert "start_camera_cleanup()" not in main_source
    assert "cleanup_orphaned_streams" not in main_source


def test_main_detaches_live_viewers_before_stopping_camera_worker():
    main_source = Path(__file__).parents[3].joinpath("app", "main.py").read_text(encoding="utf-8")
    assert main_source.index("await shutdown_all_broadcasters()") < main_source.index(
        "await stop_configured_camera_runtime()"
    )
