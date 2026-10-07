from pathlib import Path

from backend.app.services import render_browser
from backend.app.services.render_browser import DEAD_PROXY, launch_args, locate

FORBIDDEN = ("--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu-sandbox", "--no-zygote")


def test_locate_is_none_without_an_install(tmp_path: Path):
    assert locate(tmp_path) is None


def test_locate_finds_the_executable_and_version(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(render_browser, "platform_key", lambda: "linux64")
    root = tmp_path / "runtime" / "chrome-headless-shell" / "linux64"
    exe = root / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (root / "VERSION").write_text("155.0.8059.39\n")
    found = locate(tmp_path)
    assert found is not None
    assert found.executable == exe
    assert found.version == "155.0.8059.39"
    assert found.platform == "linux64"


def test_locate_requires_the_version_marker(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(render_browser, "platform_key", lambda: "win64")
    exe = (
        tmp_path
        / "runtime"
        / "chrome-headless-shell"
        / "win64"
        / "chrome-headless-shell-win64"
        / "chrome-headless-shell.exe"
    )
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    assert locate(tmp_path) is None


def test_launch_args_keep_the_sandbox_and_route_everything_through_a_dead_proxy(tmp_path: Path):
    args = launch_args(profile_dir=tmp_path / "profile", origin="127.0.0.1:41234")
    assert not any(a.startswith(f) for a in args for f in FORBIDDEN)
    assert f"--proxy-server={DEAD_PROXY}" in args
    bypass = [a for a in args if a.startswith("--proxy-bypass-list=")]
    # The exact HTTP origin: a scheme-less entry would let https:// and ws:// to the same port through.
    assert bypass == ["--proxy-bypass-list=<-loopback>;http://127.0.0.1:41234"]
    assert "--use-angle=swiftshader" in args
    assert "--enable-unsafe-swiftshader" in args
    assert f"--user-data-dir={tmp_path / 'profile'}" in args


def test_launch_args_add_a_netlog_only_when_asked(tmp_path: Path):
    assert not any(a.startswith("--log-net-log") for a in launch_args(profile_dir=tmp_path, origin="127.0.0.1:1"))
    args = launch_args(profile_dir=tmp_path, origin="127.0.0.1:1", netlog=tmp_path / "net.json")
    assert f"--log-net-log={tmp_path / 'net.json'}" in args


SERVICE_ENV = {
    "PATH": "/usr/bin",
    "LANG": "C.UTF-8",
    "SYSTEMROOT": r"C:\Windows",
    "TEMP": "/tmp",
    "DATABASE_URL": "postgresql://user:secret@db/bamdude",
    "BAMDUDE_API_KEY": "bd_secret",
    "MFA_ENCRYPTION_KEY": "k",
    "HOME": "/opt/bamdude",
}


def test_browser_env_keeps_only_the_allowlist(tmp_path: Path):
    env = render_browser.browser_env(tmp_path, environ=SERVICE_ENV, posix=False)
    assert env == {"PATH": "/usr/bin", "LANG": "C.UTF-8", "SYSTEMROOT": r"C:\Windows", "TEMP": "/tmp"}


def test_browser_env_points_home_and_xdg_into_the_attempt_profile_on_posix(tmp_path: Path):
    env = render_browser.browser_env(tmp_path, environ=SERVICE_ENV, posix=True)
    assert "DATABASE_URL" not in env and "BAMDUDE_API_KEY" not in env
    assert env["HOME"] == str(tmp_path)
    assert env["XDG_CACHE_HOME"] == str(tmp_path / ".cache")
    assert env["XDG_CONFIG_HOME"] == str(tmp_path / ".config")


def test_locate_in_takes_any_runtime_directory_name(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(render_browser, "platform_key", lambda: "linux64")
    root = tmp_path / "rt" / "chrome-headless-shell" / "linux64"
    exe = root / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (root / "VERSION").write_text("155.0.8059.39\n")
    assert render_browser.locate_in(tmp_path / "rt").executable == exe


def test_check_libs_reads_the_runtime_it_is_given(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(render_browser, "platform_key", lambda: "linux64")
    root = tmp_path / "rt" / "chrome-headless-shell" / "linux64"
    exe = root / "chrome-headless-shell-linux64" / "chrome-headless-shell"
    exe.parent.mkdir(parents=True)
    exe.write_bytes(b"")
    (root / "VERSION").write_text("155.0.8059.39\n")
    seen = {}

    def check(paths, run=None):
        seen["paths"] = paths
        return render_browser.LibraryCheck(ok=True, missing=[], errors=[])

    monkeypatch.setattr(render_browser, "check_libraries", check)
    assert render_browser.main(["check-libs", "--runtime", str(tmp_path / "rt")]) == 0
    assert seen["paths"][0] == exe
