"""Stdlib-only mock of a Bambu printer's implicit-FTPS server.

Why this is hand-rolled instead of pyftpdlib
--------------------------------------------
pyftpdlib drives its sockets through ``asyncore``, which PEP 594 deleted from
the standard library in Python 3.12. On 3.12+ it silently falls back to the
``pyasyncore`` PyPI backport, whose ``listen()`` raises ``WinError 10022`` when
the passive data channel is opened on Windows: the control channel connects and
logs in fine, then every single transfer dies with a bare ``EOFError``. Removed
stdlib modules do not come back, and a mock that only works on one interpreter
is a mock whose silence reads as success — so this is plain sockets + threads.

What it emulates is driven entirely by what
:class:`backend.app.services.bambu_ftp.BambuFTPClient` actually puts on the
wire, including three printer quirks that a textbook FTP server would get
"right" and thereby break the tests:

* **Implicit FTPS.** TLS is negotiated the instant the TCP connection is up,
  *before* the 220 banner. There is no ``AUTH TLS`` handshake.
* **``SIZE`` works in ASCII mode.** RFC 3659 lets a server refuse ``SIZE``
  while ``TYPE A`` is in effect; real printers answer anyway, and
  ``BambuFTPClient.get_file_size()`` never sends ``TYPE I`` first.
* **``AVBL``.** A Bambu extension returning the free bytes on the SD card as
  ``213 <bytes>``. Nothing else in FTP-land has it.

The data channel may be TLS *or* plaintext and the protocol does not say which
— see :meth:`_ControlSession._data_channel_is_tls` for how that is resolved.
"""

from __future__ import annotations

import os
import selectors
import socket
import ssl
import threading
import time
from datetime import datetime
from pathlib import Path

# Locale-independent month abbreviations for the ``ls -l`` listing. ``strftime
# ("%b")`` would follow LC_TIME, which is not guaranteed to be English on a
# contributor's machine, and the client parses the listing with ``strptime``.
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")

# First two bytes of any TLS record carrying a handshake: content type 0x16
# followed by the legacy record version 0x03 0xNN. Every ClientHello from
# TLS 1.0 through 1.3 starts with this, which is what makes the peek in
# :meth:`_ControlSession._data_channel_is_tls` decisive.
_TLS_HANDSHAKE_PREFIX = b"\x16\x03"

# Commands that are answered before the client has logged in.
_PRE_AUTH_COMMANDS = frozenset({"USER", "PASS", "QUIT", "FEAT", "NOOP", "SYST", "AUTH"})


class MockBambuFTPServer:
    """A single-instance implicit-FTPS server backed by a real directory tree.

    Every knob (failure injection, ``AVBL`` value) lives on the instance rather
    than on a class. The suite starts one server per test class, so class-level
    state would let an injected 550 from one class bleed into the next — which
    is exactly why the pyftpdlib version had to mint a throwaway handler
    subclass per instance.
    """

    # How long to wait for the client to open the passive data connection. The
    # client connects *before* it sends the transfer command, so in practice
    # ``accept()`` returns immediately; this only bounds a broken client.
    DATA_ACCEPT_TIMEOUT = 15.0

    # Per-recv/send timeout once a data transfer is under way.
    DATA_TIMEOUT = 60.0

    # Bound on the TLS close_notify exchange when tearing a data channel down.
    DATA_SHUTDOWN_TIMEOUT = 10.0

    # How long the control socket blocks in ``recv`` before re-checking the
    # shutdown flag. Keeps ``stop()`` prompt without a wakeup channel per
    # session.
    CONTROL_POLL_INTERVAL = 0.25

    # TLS auto-detect windows — see ``_data_channel_is_tls``.
    TLS_PEEK_TIMEOUT = 0.25
    TLS_PEEK_TIMEOUT_PROT_P = 2.0

    def __init__(
        self,
        host: str,
        port: int,
        root_dir: str,
        cert_path: str,
        key_path: str,
        access_code: str = "12345678",
    ):
        self.host = host
        self.port = port
        self.root_dir = root_dir
        self.cert_path = cert_path
        self.key_path = key_path
        self.access_code = access_code

        self.root = Path(root_dir)

        # command -> (code, message, remaining). remaining == -1 is permanent.
        self._failures: dict[str, tuple[int, str, int]] = {}
        self._avbl_bytes = 1073741824  # 1 GiB, same default as the old mock
        self._state_lock = threading.Lock()

        self._ssl_context: ssl.SSLContext | None = None
        self._listener: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._sessions: list[_ControlSession] = []
        self._sessions_lock = threading.Lock()
        self._stopping = threading.Event()
        # Wakes the accept loop out of ``select`` the moment ``stop()`` is
        # called; closing a socket another thread is blocked on is not portable.
        self._wake_r: socket.socket | None = None
        self._wake_w: socket.socket | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Bind, listen and start accepting. Returns once the port is live.

        The listen backlog is open before this returns, so a client may connect
        immediately — no post-start sleep is needed.
        """
        self._stopping.clear()

        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # Real printers speak TLS 1.2; the client floors itself there too and
        # some models cap the ceiling to 1.2 (see ``ftp_profiles``). Declaring
        # the floor keeps the mock from silently accepting a weaker handshake
        # than any printer would.
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(self.cert_path, self.key_path)
        self._ssl_context = context

        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        if os.name != "nt":
            # SO_REUSEADDR means "rebind a TIME_WAIT port" on POSIX but "let two
            # sockets fight over the same port" on Windows, so it is POSIX-only.
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((self.host, self.port))
        listener.listen(16)
        self.port = listener.getsockname()[1]
        self._listener = listener

        self._wake_r, self._wake_w = socket.socketpair()

        self._accept_thread = threading.Thread(target=self._accept_loop, name="mock-ftps-accept", daemon=True)
        self._accept_thread.start()

    def stop(self) -> None:
        """Shut everything down and join every thread we started.

        The suite creates a server per test class under xdist, so a leaked
        thread or a socket still bound to the port is a cross-test failure, not
        just untidiness.
        """
        self._stopping.set()

        if self._wake_w is not None:
            try:
                self._wake_w.send(b"\x00")
            except OSError:
                pass

        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                pass

        with self._sessions_lock:
            sessions = list(self._sessions)
        for session in sessions:
            session.shutdown()

        if self._accept_thread is not None:
            self._accept_thread.join(timeout=5)
        for session in sessions:
            session.join(timeout=5)

        for sock in (self._wake_r, self._wake_w):
            if sock is not None:
                try:
                    sock.close()
                except OSError:
                    pass

        self._listener = None
        self._accept_thread = None
        self._wake_r = None
        self._wake_w = None
        with self._sessions_lock:
            self._sessions.clear()

    def _accept_loop(self) -> None:
        listener = self._listener
        assert listener is not None and self._wake_r is not None
        try:
            with selectors.DefaultSelector() as sel:
                sel.register(listener, selectors.EVENT_READ)
                sel.register(self._wake_r, selectors.EVENT_READ)
                while not self._stopping.is_set():
                    try:
                        events = sel.select(timeout=1.0)
                    except OSError:
                        return
                    for key, _mask in events:
                        if key.fileobj is not listener:
                            return  # stop() poked the wakeup socket
                        try:
                            conn, _addr = listener.accept()
                        except OSError:
                            return
                        self._spawn_session(conn)
        except Exception:
            # This runs on a daemon thread; letting anything escape would print
            # a traceback from nowhere mid-suite instead of failing a test.
            return

    def _spawn_session(self, conn: socket.socket) -> None:
        if self._stopping.is_set():
            conn.close()
            return
        session = _ControlSession(self, conn)
        with self._sessions_lock:
            self._sessions.append(session)
        session.start()

    def _forget_session(self, session: _ControlSession) -> None:
        with self._sessions_lock:
            if session in self._sessions:
                self._sessions.remove(session)

    # -- test-facing controls ---------------------------------------------

    def inject_failure(self, command: str, code: int, message: str, count: int = -1) -> None:
        """Make *command* answer ``<code> <message>`` instead of doing its job.

        Args:
            command: FTP verb (RETR, STOR, DELE, CWD, LIST, SIZE, PASS, ...).
            code: FTP response code (e.g. 550, 553).
            message: Response text.
            count: How many times to fail; -1 (default) is permanent.
        """
        with self._state_lock:
            self._failures[command.upper()] = (code, message, count)

    def clear_failures(self) -> None:
        """Remove all injected failures."""
        with self._state_lock:
            self._failures.clear()

    def set_avbl_bytes(self, n: int) -> None:
        """Set the value the Bambu-specific ``AVBL`` command reports."""
        with self._state_lock:
            self._avbl_bytes = n

    def _consume_failure(self, command: str) -> tuple[int, str] | None:
        """Pop one injected failure for *command*, or ``None`` if not armed."""
        with self._state_lock:
            entry = self._failures.get(command)
            if entry is None:
                return None
            code, message, remaining = entry
            if remaining == 0:
                return None
            if remaining > 0:
                remaining -= 1
                if remaining == 0:
                    del self._failures[command]
                else:
                    self._failures[command] = (code, message, remaining)
            return code, message

    @property
    def avbl_bytes(self) -> int:
        with self._state_lock:
            return self._avbl_bytes

    # -- filesystem inspection --------------------------------------------

    def add_file(self, relative_path: str, content: bytes = b"") -> None:
        """Add a file to the server's filesystem."""
        full_path = self.root / relative_path.lstrip("/")
        full_path.parent.mkdir(parents=True, exist_ok=True)
        full_path.write_bytes(content)

    def add_directory(self, relative_path: str) -> None:
        """Create a directory in the server's filesystem."""
        (self.root / relative_path.lstrip("/")).mkdir(parents=True, exist_ok=True)

    def file_exists(self, relative_path: str) -> bool:
        """Check if a file exists on the server."""
        return (self.root / relative_path.lstrip("/")).is_file()

    def read_file(self, relative_path: str) -> bytes:
        """Read file content from the server's filesystem."""
        return (self.root / relative_path.lstrip("/")).read_bytes()


class _ControlSession:
    """One FTP control connection, serviced start-to-finish by one thread.

    Transfers run inline on that same thread: FTP is strictly request/response
    on the control channel while a transfer is in flight, so there is nothing
    to overlap and a second thread would only add teardown races.
    """

    def __init__(self, server: MockBambuFTPServer, conn: socket.socket):
        self._server = server
        self._raw = conn
        self._sock: socket.socket | None = None
        self._thread = threading.Thread(target=self._run, name="mock-ftps-session", daemon=True)
        self._buffer = b""

        self._authenticated = False
        self._user: str | None = None
        self._cwd = "/"
        self._type = "A"
        self._prot_p = False
        self._pasv: socket.socket | None = None
        self._rename_from: Path | None = None
        # Sticky: once a client has shown us a plaintext data channel we stop
        # granting it the long PROT-P detection window (see below).
        self._seen_plaintext_data = False

    def start(self) -> None:
        self._thread.start()

    def join(self, timeout: float | None = None) -> None:
        self._thread.join(timeout=timeout)

    def shutdown(self) -> None:
        """Force the session closed from another thread (server ``stop()``)."""
        for sock in (self._sock or self._raw, self._pasv):
            if sock is None:
                continue
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass

    # -- plumbing ----------------------------------------------------------

    def _run(self) -> None:
        try:
            context = self._server._ssl_context
            assert context is not None
            self._raw.settimeout(self._server.DATA_ACCEPT_TIMEOUT)
            # Implicit FTPS: TLS first, banner second. An explicit-FTPS server
            # would send 220 in the clear and wait for AUTH TLS; the client
            # wraps its socket before reading a single byte, so doing that here
            # would deadlock rather than fail loudly.
            self._sock = context.wrap_socket(self._raw, server_side=True)
            self._sock.settimeout(self._server.CONTROL_POLL_INTERVAL)
            self._respond(220, "BamDude mock Bambu FTP server ready.")
            self._command_loop()
        except (OSError, ValueError):
            # OSError covers ssl.SSLError and socket timeouts; a dead control
            # connection is an ordinary end-of-session here, not a test failure.
            pass
        finally:
            self._close_pasv()
            for sock in (self._sock, self._raw):
                if sock is None:
                    continue
                try:
                    sock.close()
                except OSError:
                    pass
            self._server._forget_session(self)

    def _command_loop(self) -> None:
        while not self._server._stopping.is_set():
            line = self._read_line()
            if line is None:
                return
            if not line:
                continue
            command, _, argument = line.partition(" ")
            command = command.upper()
            argument = argument.strip()

            if command == "QUIT":
                self._respond(221, "Goodbye.")
                return

            if not self._authenticated and command not in _PRE_AUTH_COMMANDS:
                self._respond(530, "Please login with USER and PASS.")
                continue

            injected = self._server._consume_failure(command)
            if injected is not None:
                code, message = injected
                # A transfer command may already have a passive listener with
                # the client's socket queued on it; drop it so the client's
                # ``ntransfercmd`` cleanup is the only thing left to do.
                self._close_pasv()
                self._respond(code, message)
                continue

            handler = getattr(self, f"_cmd_{command.lower()}", None)
            if handler is None:
                self._respond(502, f"Command {command} not implemented.")
                continue
            try:
                handler(argument)
            except OSError:
                raise  # control channel is gone; let _run tear the session down
            except Exception as exc:
                # A bug in a handler should read as an FTP error the client can
                # report, not as a session that silently stops answering.
                self._respond(451, f"Local error: {exc}")

    def _read_line(self) -> str | None:
        """Read one CRLF-terminated command, or ``None`` when the peer is gone.

        Polls with a short socket timeout instead of blocking forever so that
        ``MockBambuFTPServer.stop()`` never has to rely on closing a socket a
        thread is parked in — behaviour that differs between POSIX and Windows.
        """
        assert self._sock is not None
        while b"\n" not in self._buffer:
            if self._server._stopping.is_set():
                return None
            try:
                chunk = self._sock.recv(4096)
            except TimeoutError:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self._buffer += chunk
        raw, _, self._buffer = self._buffer.partition(b"\n")
        return raw.rstrip(b"\r").decode("utf-8", "replace")

    def _respond(self, code: int, message: str) -> None:
        self._send_control(f"{code} {message}\r\n")

    def _send_control(self, text: str) -> None:
        """Write to the control channel with a send-sized timeout.

        The socket normally carries the short ``CONTROL_POLL_INTERVAL`` so the
        read loop can notice ``stop()``. That is far too tight for a write: a
        TLS ``sendall`` that times out part-way through a record leaves the
        session unable to write anything else, so the timeout is widened for
        the duration of the send and restored afterwards.
        """
        sock = self._sock
        assert sock is not None
        sock.settimeout(self._server.DATA_TIMEOUT)
        try:
            sock.sendall(text.encode())
        finally:
            sock.settimeout(self._server.CONTROL_POLL_INTERVAL)

    # -- path handling -----------------------------------------------------

    def _normalize(self, path: str) -> str:
        """Resolve *path* against the session cwd into a canonical FTP path."""
        combined = path if path.startswith("/") else f"{self._cwd.rstrip('/')}/{path}"
        parts: list[str] = []
        for segment in combined.split("/"):
            if segment in ("", "."):
                continue
            if segment == "..":
                if parts:
                    parts.pop()
                continue
            parts.append(segment)
        return "/" + "/".join(parts)

    def _local(self, path: str) -> Path | None:
        """Map an FTP path to a real path, or ``None`` if it escapes the root."""
        ftp_path = self._normalize(path)
        local = self._server.root.joinpath(*[p for p in ftp_path.split("/") if p])
        try:
            local.resolve().relative_to(self._server.root.resolve())
        except (OSError, ValueError):
            return None
        return local

    # -- data channel ------------------------------------------------------

    def _close_pasv(self) -> None:
        if self._pasv is not None:
            try:
                self._pasv.close()
            except OSError:
                pass
            self._pasv = None

    def _data_channel_is_tls(self, conn: socket.socket) -> bool:
        """Decide whether this data connection is TLS, without consuming it.

        ``PROT`` cannot answer this. The A1 / A1 Mini path in
        :class:`ImplicitFTP_TLS` wraps the data socket only when
        ``skip_session_reuse`` is false, so a printer profile can announce a
        protected data channel and then hand over plaintext bytes — and the
        server has to cope either way.

        What *is* reliable is that a TLS client always speaks first: OpenSSL
        emits the ClientHello the moment the socket is wrapped, even for RETR
        where the payload flows the other way. So peek (``MSG_PEEK`` leaves the
        bytes in the kernel buffer for the real handshake) and look for a TLS
        handshake record header. Nothing within the window, or a first byte
        that is not 0x16, means plaintext.

        The window is short because a plaintext RETR pays it in full — the
        client sends nothing, so we always wait it out. It is stretched when
        ``PROT P`` is in effect, since a ClientHello really is expected then and
        guessing plaintext there would corrupt the transfer; once a client has
        proven it sends plaintext under PROT P, the long window is dropped so
        it is paid at most once per session.
        """
        if self._prot_p and not self._seen_plaintext_data:
            window = self._server.TLS_PEEK_TIMEOUT_PROT_P
        else:
            window = self._server.TLS_PEEK_TIMEOUT
        deadline = time.monotonic() + window

        peeked = b""
        while len(peeked) < len(_TLS_HANDSHAKE_PREFIX):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                conn.settimeout(remaining)
                peeked = conn.recv(len(_TLS_HANDSHAKE_PREFIX), socket.MSG_PEEK)
            except (TimeoutError, OSError):
                break
            if not peeked:
                break  # client closed without sending: nothing to handshake with
            if not _TLS_HANDSHAKE_PREFIX.startswith(peeked):
                break  # first byte already rules TLS out
            if len(peeked) < len(_TLS_HANDSHAKE_PREFIX):
                # Only a partial record header is buffered; give the rest of the
                # segment a moment rather than spinning on MSG_PEEK.
                time.sleep(0.005)

        if peeked.startswith(_TLS_HANDSHAKE_PREFIX):
            return True
        self._seen_plaintext_data = True
        return False

    def _open_data(self) -> socket.socket | None:
        """Send 150, accept the passive connection, negotiate TLS if offered."""
        listener, self._pasv = self._pasv, None
        if listener is None:
            self._respond(425, "Use PASV first.")
            return None

        # 150 must precede the accept-and-handshake dance: the client only wraps
        # its data socket after it has read the preliminary reply, so a server
        # that handshakes first would wait forever.
        self._respond(150, "Opening data connection.")
        try:
            listener.settimeout(self._server.DATA_ACCEPT_TIMEOUT)
            conn, _addr = listener.accept()
        except OSError:
            self._respond(425, "Cannot open data connection.")
            return None
        finally:
            try:
                listener.close()
            except OSError:
                pass

        try:
            use_tls = self._data_channel_is_tls(conn)
            conn.settimeout(self._server.DATA_TIMEOUT)
            if use_tls:
                assert self._server._ssl_context is not None
                conn = self._server._ssl_context.wrap_socket(conn, server_side=True)
                conn.settimeout(self._server.DATA_TIMEOUT)
        except OSError:
            try:
                conn.close()
            except OSError:
                pass
            self._respond(426, "Data channel TLS negotiation failed.")
            return None
        return conn

    def _close_data(self, conn: socket.socket) -> None:
        """Tear a data connection down the way the client expects.

        On a TLS channel the client calls ``unwrap()`` (both ``ftplib`` and
        ``bambu_ftp._graceful_close_data_conn`` do) and blocks until it sees our
        close_notify, so we must send one instead of dropping the TCP socket. On
        a download our close_notify is also what signals EOF to the reader.
        """
        try:
            if isinstance(conn, ssl.SSLSocket):
                try:
                    conn.settimeout(self._server.DATA_SHUTDOWN_TIMEOUT)
                    conn.unwrap()
                except (OSError, ValueError):
                    pass
        finally:
            try:
                conn.close()
            except OSError:
                pass

    def _send_data(self, payload: bytes) -> None:
        """Run a server→client transfer, answering 226 or 426 on the control channel."""
        conn = self._open_data()
        if conn is None:
            return
        try:
            if payload:
                conn.sendall(payload)
        except OSError:
            self._close_data(conn)
            self._respond(426, "Transfer aborted.")
            return
        self._close_data(conn)
        self._respond(226, "Transfer complete.")

    # -- authentication ----------------------------------------------------

    def _cmd_user(self, argument: str) -> None:
        self._user = argument
        self._authenticated = False
        self._respond(331, f"Password required for {argument}.")

    def _cmd_pass(self, argument: str) -> None:
        if self._user == "bblp" and argument == self._server.access_code:
            self._authenticated = True
            self._respond(230, "Login successful.")
        else:
            self._respond(530, "Login incorrect.")

    # -- session / no-op commands -----------------------------------------

    def _cmd_noop(self, argument: str) -> None:
        self._respond(200, "NOOP ok.")

    def _cmd_syst(self, argument: str) -> None:
        self._respond(215, "UNIX Type: L8")

    def _cmd_opts(self, argument: str) -> None:
        self._respond(200, "OK.")

    def _cmd_feat(self, argument: str) -> None:
        self._send_control("211-Features:\r\n SIZE\r\n AVBL\r\n PASV\r\n PBSZ\r\n PROT\r\n211 End\r\n")

    def _cmd_stat(self, argument: str) -> None:
        # Only reached as ``get_storage_info``'s fallback when AVBL fails.
        self._respond(211, "BamDude mock FTP server status: ok.")

    def _cmd_type(self, argument: str) -> None:
        code = argument.upper()[:1] or "A"
        if code not in ("A", "I", "L"):
            self._respond(504, f"Unsupported type {argument}.")
            return
        self._type = code
        self._respond(200, f"Type set to {code}.")

    def _cmd_pbsz(self, argument: str) -> None:
        # Implicit FTPS has no protection-buffer negotiation to do; accept and
        # move on. ``ftplib.prot_c()`` never sends PBSZ at all, so refusing PROT
        # without a preceding PBSZ (as a strict RFC 2228 server would) breaks
        # the A1 clear-data-channel path with a 503.
        self._respond(200, "PBSZ=0")

    def _cmd_prot(self, argument: str) -> None:
        level = argument.upper()[:1]
        if level == "P":
            self._prot_p = True
            self._respond(200, "PROT now Private.")
        elif level == "C":
            self._prot_p = False
            self._respond(200, "PROT now Clear.")
        else:
            self._respond(504, f"PROT {argument} unsupported.")

    def _cmd_avbl(self, argument: str) -> None:
        """Bambu extension: free bytes on the SD card."""
        self._respond(213, str(self._server.avbl_bytes))

    # -- navigation --------------------------------------------------------

    def _cmd_pwd(self, argument: str) -> None:
        self._respond(257, f'"{self._cwd}" is the current directory')

    _cmd_xpwd = _cmd_pwd

    def _cmd_cwd(self, argument: str) -> None:
        target = self._normalize(argument or "/")
        local = self._local(argument or "/")
        if local is None or not local.is_dir():
            self._respond(550, f"{argument}: No such file or directory.")
            return
        self._cwd = target
        self._respond(250, "Directory successfully changed.")

    def _cmd_cdup(self, argument: str) -> None:
        self._cmd_cwd("..")

    def _cmd_mkd(self, argument: str) -> None:
        local = self._local(argument)
        if local is None or local.exists():
            self._respond(550, f"{argument}: Cannot create directory.")
            return
        try:
            local.mkdir(parents=True)
        except OSError as exc:
            self._respond(550, f"{argument}: {exc}")
            return
        self._respond(257, f'"{self._normalize(argument)}" created')

    def _cmd_rmd(self, argument: str) -> None:
        local = self._local(argument)
        if local is None or not local.is_dir():
            self._respond(550, f"{argument}: No such directory.")
            return
        try:
            local.rmdir()
        except OSError as exc:
            self._respond(550, f"{argument}: {exc}")
            return
        self._respond(250, "Directory removed.")

    # -- passive mode ------------------------------------------------------

    def _cmd_pasv(self, argument: str) -> None:
        assert self._sock is not None
        self._close_pasv()
        host = self._sock.getsockname()[0]
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind((host, 0))
        listener.listen(1)
        self._pasv = listener
        port = listener.getsockname()[1]
        quads = host.replace(".", ",")
        self._respond(227, f"Entering Passive Mode ({quads},{port >> 8},{port & 0xFF}).")

    # -- listings ----------------------------------------------------------

    def _listing_target(self, argument: str) -> Path | None:
        # Clients may pass ls-style flags ("LIST -la"); the only argument we
        # care about is a path.
        parts = [token for token in argument.split() if not token.startswith("-")]
        return self._local(parts[0] if parts else self._cwd)

    @staticmethod
    def _format_entry(entry: Path) -> str:
        info = entry.stat()
        is_dir = entry.is_dir()
        perms = "drwxr-xr-x" if is_dir else "-rw-r--r--"
        size = 0 if is_dir else info.st_size
        stamp = datetime.fromtimestamp(info.st_mtime)
        when = f"{_MONTHS[stamp.month - 1]} {stamp.day:02d} {stamp.hour:02d}:{stamp.minute:02d}"
        # BambuFTPClient.list_files() splits on whitespace and needs >= 9 fields
        # with size at index 4 and the name from index 8 on, so keep the classic
        # ``ls -l`` column layout.
        return f"{perms}   1 bblp     bblp     {size:>12} {when} {entry.name}"

    def _cmd_list(self, argument: str) -> None:
        target = self._listing_target(argument)
        if target is None or not target.is_dir():
            self._respond(550, f"{argument}: No such file or directory.")
            self._close_pasv()
            return
        lines = [self._format_entry(child) for child in sorted(target.iterdir(), key=lambda p: p.name)]
        payload = "".join(f"{line}\r\n" for line in lines).encode("utf-8", "replace")
        self._send_data(payload)

    def _cmd_nlst(self, argument: str) -> None:
        target = self._listing_target(argument)
        if target is None or not target.is_dir():
            self._respond(550, f"{argument}: No such file or directory.")
            self._close_pasv()
            return
        names = sorted(child.name for child in target.iterdir())
        payload = "".join(f"{name}\r\n" for name in names).encode("utf-8", "replace")
        self._send_data(payload)

    # -- file transfer -----------------------------------------------------

    def _cmd_retr(self, argument: str) -> None:
        local = self._local(argument)
        if local is None or not local.is_file():
            self._respond(550, f"{argument}: No such file or directory.")
            self._close_pasv()
            return
        try:
            payload = local.read_bytes()
        except OSError as exc:
            self._respond(550, f"{argument}: {exc}")
            self._close_pasv()
            return
        self._send_data(payload)

    def _cmd_stor(self, argument: str) -> None:
        local = self._local(argument)
        if local is None or not local.parent.is_dir() or local.is_dir():
            self._respond(550, f"{argument}: Could not create file.")
            self._close_pasv()
            return

        conn = self._open_data()
        if conn is None:
            return

        received = bytearray()
        aborted = False
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                received += chunk
        except OSError:
            aborted = True
        finally:
            self._close_data(conn)

        try:
            local.write_bytes(bytes(received))
        except OSError as exc:
            self._respond(451, f"{argument}: {exc}")
            return

        # The file is on disk *before* 226 goes out. The client's upload path
        # treats 226 as "it landed on the SD card" and downloads it back
        # immediately in several tests, so answering first would be a race we
        # invented rather than one a printer has.
        if aborted:
            self._respond(426, "Transfer aborted.")
        else:
            self._respond(226, "Transfer complete.")

    def _cmd_dele(self, argument: str) -> None:
        local = self._local(argument)
        if local is None or not local.is_file():
            self._respond(550, f"{argument}: No such file or directory.")
            return
        try:
            local.unlink()
        except OSError as exc:
            self._respond(550, f"{argument}: {exc}")
            return
        self._respond(250, "Delete operation successful.")

    def _cmd_size(self, argument: str) -> None:
        # Deliberately answers in ASCII mode too. RFC 3659 permits a 550 while
        # TYPE A is in effect, but real printers reply and
        # ``BambuFTPClient.get_file_size()`` never sends TYPE I first, so a
        # correct-by-the-book refusal here would only break the client.
        local = self._local(argument)
        if local is None or not local.is_file():
            self._respond(550, f"{argument} is not retrievable.")
            return
        self._respond(213, str(local.stat().st_size))

    def _cmd_rnfr(self, argument: str) -> None:
        local = self._local(argument)
        if local is None or not local.exists():
            self._respond(550, f"{argument}: No such file or directory.")
            return
        self._rename_from = local
        self._respond(350, "Ready for destination name.")

    def _cmd_rnto(self, argument: str) -> None:
        source, self._rename_from = self._rename_from, None
        target = self._local(argument)
        if source is None or target is None:
            self._respond(503, "RNFR required first.")
            return
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
        except OSError as exc:
            self._respond(550, f"{argument}: {exc}")
            return
        self._respond(250, "Rename successful.")
