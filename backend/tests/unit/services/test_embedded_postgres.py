"""The lifecycle layer around the bundled PostgreSQL (services/embedded_postgres.py).

Everything here runs without a server: the tools are stubbed at the module's
own seams (``_run``, ``is_running``, ``_wait_ready`` …). The one test that
starts a real server lives in tests/integration/test_embedded_postgres_live.py.
"""

import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.core.config import settings
from backend.app.services import embedded_postgres as ep


@pytest.fixture
def embedded(tmp_path, monkeypatch):
    """settings as DATABASE_URL=embedded would leave them, pointed at tmp_path."""
    pgdata = tmp_path / "postgres" / "18"
    password_file = tmp_path / "postgres" / "password"
    password_file.parent.mkdir(parents=True)
    password_file.write_text("pw-for-tests\n", encoding="utf-8")
    monkeypatch.setattr(settings, "embedded_postgres", True)
    monkeypatch.setattr(settings, "embedded_pg_data_dir", pgdata)
    monkeypatch.setattr(settings, "embedded_pg_password_file", password_file)
    monkeypatch.setattr(settings, "embedded_pg_port", 54329)
    monkeypatch.setattr(settings, "embedded_pg_max_connections", 120)
    return pgdata


def _fake_run(calls: list, code: int = 0, out: str = ""):
    async def run(*args, env=None, timeout=120.0):
        calls.append([Path(a).name if str(a).endswith((".exe", "pg_ctl", "initdb")) else str(a) for a in args])
        return code, out

    return run


class TestConf:
    def test_conf_pins_localhost_port_and_preloads_pg_stat_statements(self, embedded):
        embedded.mkdir(parents=True)
        ep._write_conf()
        conf = (embedded / "bamdude.conf").read_text(encoding="utf-8")
        assert "listen_addresses = '127.0.0.1'" in conf
        assert "port = 54329" in conf
        assert "unix_socket_directories = ''" in conf
        assert "shared_preload_libraries = 'pg_stat_statements'" in conf
        assert "max_connections = 120" in conf
        # the server's clock agrees with the naive-UTC timestamps BamDude stores
        assert "timezone = 'UTC'" in conf and "log_timezone = 'UTC'" in conf

    def test_conf_never_goes_below_the_pool_plus_margin(self, embedded, monkeypatch):
        embedded.mkdir(parents=True)
        monkeypatch.setattr(settings, "embedded_pg_max_connections", 5)
        ep._write_conf()
        assert "max_connections = 120" in (embedded / "bamdude.conf").read_text(encoding="utf-8")


class TestRefuseOtherMajor:
    def test_no_data_directory_is_fine(self, embedded):
        ep._refuse_other_major()

    def test_same_major_is_fine(self, embedded, monkeypatch):
        embedded.mkdir(parents=True)
        (embedded / "PG_VERSION").write_text("18\n", encoding="utf-8")
        monkeypatch.setattr(ep, "bundled_major", lambda: "18")
        ep._refuse_other_major()

    def test_another_major_is_refused_never_opened(self, embedded, monkeypatch):
        embedded.mkdir(parents=True)
        (embedded / "PG_VERSION").write_text("17\n", encoding="utf-8")
        monkeypatch.setattr(ep, "bundled_major", lambda: "18")
        with pytest.raises(ep.EmbeddedPostgresError) as exc:
            ep._refuse_other_major()
        assert "PostgreSQL 17" in str(exc.value) and "PostgreSQL 18" in str(exc.value)


class TestRunningPort:
    def test_reads_line_four_of_postmaster_pid(self, embedded):
        embedded.mkdir(parents=True)
        (embedded / "postmaster.pid").write_text("123\n/data\n1700000000\n55729\n\n127.0.0.1\n", encoding="utf-8")
        assert ep._running_port() == 55729

    def test_missing_or_short_file_is_none(self, embedded):
        assert ep._running_port() is None
        embedded.mkdir(parents=True)
        (embedded / "postmaster.pid").write_text("123\n", encoding="utf-8")
        assert ep._running_port() is None


class TestRun:
    async def test_captures_exit_code_and_output_through_a_file(self):
        code, out = await ep._run(sys.executable, "-c", "import sys; print('hello'); sys.exit(3)")
        assert code == 3
        assert "hello" in out

    async def test_a_hung_tool_is_killed_and_reported(self):
        with pytest.raises(ep.EmbeddedPostgresError) as exc:
            await ep._run(sys.executable, "-c", "import time; time.sleep(30)", timeout=0.5)
        assert "timed out" in str(exc.value)

    async def test_on_windows_no_tool_shares_bamdude_console(self, monkeypatch):
        """A console delivers Ctrl+C to every process attached to it — the
        server pg_ctl spawns included, which then shuts itself down (racing its
        own checkpointer) before the lifespan reaches stop(). pg_ctl setsid()s
        on Unix; on Windows the tools get a console of their own."""
        seen = {}

        class Proc:
            async def wait(self):
                return 0

            def kill(self):
                pass

        async def fake_exec(*args, **kwargs):
            seen.update(kwargs)
            return Proc()

        monkeypatch.setattr(ep.asyncio, "create_subprocess_exec", fake_exec)
        await ep._run("pg_ctl", "start")
        expected = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        assert seen["creationflags"] == expected


class TestStart:
    async def test_not_embedded_is_a_no_op(self, monkeypatch):
        monkeypatch.setattr(settings, "embedded_postgres", False)
        called = []
        monkeypatch.setattr(ep, "_initdb", lambda: called.append("initdb"))
        await ep.start()
        assert called == []

    async def test_fresh_directory_runs_initdb_then_starts(self, embedded, monkeypatch):
        order = []

        async def initdb():
            order.append("initdb")
            embedded.mkdir(parents=True, exist_ok=True)

        async def is_running():
            return False

        async def start_server(pgdata):
            order.append("start")

        async def ready():
            order.append("ready")

        async def ensure_db():
            order.append("createdb")

        async def psql(sql, database="postgres"):
            order.append(f"psql:{sql[:6]}")
            return ""

        monkeypatch.setattr(ep, "_initdb", initdb)
        monkeypatch.setattr(ep, "is_running", is_running)
        monkeypatch.setattr(ep, "_start_server", start_server)
        monkeypatch.setattr(ep, "_wait_ready", ready)
        monkeypatch.setattr(ep, "_ensure_database", ensure_db)
        monkeypatch.setattr(ep, "_psql", psql)
        monkeypatch.setattr(ep, "bundled_major", lambda: "18")
        await ep.start()
        assert order == ["initdb", "start", "ready", "createdb", "psql:CREATE"]
        assert (embedded / "bamdude.conf").exists()

    async def test_a_running_server_on_the_configured_port_is_reused(self, embedded, monkeypatch):
        embedded.mkdir(parents=True)
        (embedded / "PG_VERSION").write_text("18\n", encoding="utf-8")
        (embedded / "postmaster.pid").write_text("1\n/d\n0\n54329\n", encoding="utf-8")
        order = []

        async def is_running():
            return True

        async def start_server(pgdata):
            order.append("start")

        async def stop():
            order.append("stop")

        async def noop(*a, **k):
            return ""

        monkeypatch.setattr(ep, "is_running", is_running)
        monkeypatch.setattr(ep, "_start_server", start_server)
        monkeypatch.setattr(ep, "stop", stop)
        monkeypatch.setattr(ep, "_wait_ready", noop)
        monkeypatch.setattr(ep, "_ensure_database", noop)
        monkeypatch.setattr(ep, "_psql", noop)
        monkeypatch.setattr(ep, "bundled_major", lambda: "18")
        await ep.start()
        assert order == []

    async def test_a_running_server_on_another_port_is_restarted(self, embedded, monkeypatch):
        embedded.mkdir(parents=True)
        (embedded / "PG_VERSION").write_text("18\n", encoding="utf-8")
        (embedded / "postmaster.pid").write_text("1\n/d\n0\n55729\n", encoding="utf-8")
        order = []

        async def is_running():
            return True

        async def start_server(pgdata):
            order.append("start")

        async def stop():
            order.append("stop")

        async def noop(*a, **k):
            return ""

        monkeypatch.setattr(ep, "is_running", is_running)
        monkeypatch.setattr(ep, "_start_server", start_server)
        monkeypatch.setattr(ep, "stop", stop)
        monkeypatch.setattr(ep, "_wait_ready", noop)
        monkeypatch.setattr(ep, "_ensure_database", noop)
        monkeypatch.setattr(ep, "_psql", noop)
        monkeypatch.setattr(ep, "bundled_major", lambda: "18")
        await ep.start()
        assert order == ["stop", "start"]

    async def test_a_directory_of_another_major_stops_everything_first(self, embedded, monkeypatch):
        embedded.mkdir(parents=True)
        (embedded / "PG_VERSION").write_text("17\n", encoding="utf-8")
        monkeypatch.setattr(ep, "bundled_major", lambda: "18")
        touched = []
        monkeypatch.setattr(ep, "_initdb", lambda: touched.append("initdb"))
        with pytest.raises(ep.EmbeddedPostgresError):
            await ep.start()
        assert touched == []


class TestExternalService:
    """EMBEDDED_PG_EXTERNAL_SERVICE: the server is its own OS service (the
    Windows installer's BamDudePostgres). BamDude connects but never owns the
    lifecycle — no initdb, no conf, no start, no stop."""

    async def test_start_connects_and_ensures_db_but_never_touches_lifecycle(self, embedded, monkeypatch):
        monkeypatch.setattr(settings, "embedded_pg_external_service", True)
        order = []

        async def ready():
            order.append("ready")

        async def ensure_db():
            order.append("createdb")

        async def psql(sql, database="postgres"):
            order.append(f"psql:{sql[:6]}")
            return ""

        def boom(*a, **k):
            raise AssertionError("lifecycle tool must not run in external-service mode")

        monkeypatch.setattr(ep, "_wait_ready", ready)
        monkeypatch.setattr(ep, "_ensure_database", ensure_db)
        monkeypatch.setattr(ep, "_psql", psql)
        monkeypatch.setattr(ep, "_initdb", boom)
        monkeypatch.setattr(ep, "_start_server", boom)
        monkeypatch.setattr(ep, "stop", boom)
        monkeypatch.setattr(ep, "_write_conf", boom)
        monkeypatch.setattr(ep, "bundled_major", lambda: "18")
        await ep.start()
        assert order == ["ready", "createdb", "psql:CREATE"]
        # no conf was written for a server we don't own
        assert not (embedded / "bamdude.conf").exists()

    async def test_stop_is_a_no_op(self, embedded, monkeypatch):
        monkeypatch.setattr(settings, "embedded_pg_external_service", True)

        def boom(*a, **k):
            raise AssertionError("stop must not run pg_ctl in external-service mode")

        monkeypatch.setattr(ep, "is_running", boom)
        monkeypatch.setattr(ep, "_run", boom)
        await ep.stop()


class TestStop:
    async def test_not_running_is_a_no_op(self, embedded, monkeypatch):
        calls = []

        async def is_running():
            return False

        monkeypatch.setattr(ep, "is_running", is_running)
        monkeypatch.setattr(ep, "_run", _fake_run(calls))
        await ep.stop()
        assert calls == []

    async def test_running_gets_a_fast_never_immediate_shutdown(self, embedded, monkeypatch):
        calls = []

        async def is_running():
            return True

        monkeypatch.setattr(ep, "is_running", is_running)
        monkeypatch.setattr(ep, "_run", _fake_run(calls))
        monkeypatch.setattr(ep, "_bin", lambda name: Path(f"/bin/{name}"))
        await ep.stop()
        assert len(calls) == 1
        assert "stop" in calls[0] and "fast" in calls[0] and "immediate" not in calls[0]


class TestBin:
    def test_a_missing_tool_is_a_clear_error(self, monkeypatch, tmp_path):
        import embedded_postgres._commands as commands

        monkeypatch.setattr(commands, "POSTGRES_BIN_PATH", tmp_path)
        with pytest.raises(ep.EmbeddedPostgresError) as exc:
            ep._bin("initdb")
        assert "initdb" in str(exc.value)

    def test_bundled_major_is_a_number(self):
        assert ep.bundled_major().isdigit()
