"""Tailscale presence detection for virtual printers (#1070, post-rip-out).

Reports whether ``tailscaled`` is reachable and surfaces the host's Tailscale
IPs and FQDN so the UI can show users which IP / hostname to paste into the
slicer when they want to reach a VP over Tailscale.

Historical note: this module previously provisioned Let's Encrypt certs via
``tailscale cert`` so the slicer would not need a manual CA import. That
path was removed because BambuStudio / OrcaSlicer's printer-MQTT trust path
validates only against the bundled BBL CA store (not the system trust
store), so LE-signed certs are rejected regardless of hostname/IP. The
self-signed CA flow (with one-time ``bbl_ca.crt`` import into the slicer)
is the only viable trust mechanism; Tailscale's role is now strictly
network reach — same trust burden as LAN.
"""

import asyncio
import json
import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

# Minimal environment for the tailscale subprocess — passes OS/shell variables
# the binary needs to locate its socket and config, but strips application
# secrets (JWT keys, DB URLs, SMTP passwords, etc.) that the subprocess has
# no need for.
_SUBPROCESS_ENV: dict[str, str] = {
    k: v
    for k, v in os.environ.items()
    if k
    in {
        "PATH",
        "HOME",
        "USER",
        "USERNAME",
        "LOGNAME",
        # Windows equivalents
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "SYSTEMROOT",
        "WINDIR",
        "COMPUTERNAME",
        "TEMP",
        "TMP",
        # Linux XDG dirs used by tailscale for socket/config
        "XDG_RUNTIME_DIR",
        "XDG_CONFIG_HOME",
    }
}


@dataclass
class TailscaleStatus:
    """Runtime Tailscale availability and identity (purely informational)."""

    available: bool
    hostname: str  # "myhost"
    tailnet_name: str  # "tailnetname.ts.net"
    fqdn: str  # "myhost.tailnetname.ts.net"
    tailscale_ips: list[str] = field(default_factory=list)
    error: str | None = None


class TailscaleService:
    """Wraps ``tailscale status --json`` for VP UI display.

    All methods are safe to call when Tailscale is absent — they return
    sensible defaults and never raise exceptions.
    """

    _docker_hint_logged: bool = False

    @classmethod
    def _log_docker_socket_hint(cls) -> None:
        """Log a one-time hint when running in Docker without the Tailscale socket mounted.

        Fires in both states: (a) tailscale binary missing and (b) binary present
        but the host socket isn't mounted into the container. The binary alone
        can't talk to the daemon — the host's tailscaled socket needs to be
        volume-mounted in docker-compose.yml.
        """
        if cls._docker_hint_logged:
            return
        if Path("/.dockerenv").exists() and not Path("/var/run/tailscale/tailscaled.sock").exists():
            logger.info(
                "Running in Docker but /var/run/tailscale/tailscaled.sock is not mounted. "
                "Add `- /var/run/tailscale/tailscaled.sock:/var/run/tailscale/tailscaled.sock` "
                "to docker-compose.yml (under volumes:) and run Tailscale on the host to "
                "surface the tailnet IP/hostname on the VP card."
            )
            cls._docker_hint_logged = True

    async def _run_tailscale(self, *args: str, timeout: float = 30.0) -> tuple[int | None, bytes, bytes]:
        """Run a tailscale subcommand and return ``(returncode, stdout, stderr)``.

        Resolves the binary to an absolute path to guard against PATH hijacking.
        Raises ``OSError`` if the binary cannot be found or launched. Raises
        ``asyncio.TimeoutError`` if the subprocess exceeds the timeout.
        """
        binary = shutil.which("tailscale")
        if not binary:
            raise OSError("tailscale binary not found")
        process = await asyncio.create_subprocess_exec(
            binary,
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=_SUBPROCESS_ENV,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            raise
        return process.returncode, stdout, stderr

    async def get_status(self) -> TailscaleStatus:
        """Query Tailscale status and return machine identity.

        Runs ``tailscale status --json``. Returns ``TailscaleStatus(available=False)``
        if the binary is missing, the daemon is not running, or any other error
        occurs.
        """
        if not shutil.which("tailscale"):
            self._log_docker_socket_hint()
            return TailscaleStatus(
                available=False,
                hostname="",
                tailnet_name="",
                fqdn="",
                error="tailscale binary not found",
            )

        try:
            returncode, stdout, stderr = await self._run_tailscale("status", "--json", timeout=5.0)
        except OSError as e:
            return TailscaleStatus(
                available=False,
                hostname="",
                tailnet_name="",
                fqdn="",
                error=str(e),
            )

        if returncode is None or returncode != 0:
            # Binary is present but the daemon socket is unreachable (e.g.
            # Docker without the socket mount) — log the actionable hint
            # rather than just the opaque CLI stderr.
            self._log_docker_socket_hint()
            return TailscaleStatus(
                available=False,
                hostname="",
                tailnet_name="",
                fqdn="",
                error=stderr.decode(errors="replace").strip(),
            )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError as e:
            return TailscaleStatus(
                available=False,
                hostname="",
                tailnet_name="",
                fqdn="",
                error=f"JSON parse error: {e}",
            )

        self_info = data.get("Self", {})

        # DNSName includes trailing dot: "myhost.tailnetname.ts.net."
        fqdn = self_info.get("DNSName", "").rstrip(".")
        if not fqdn:
            return TailscaleStatus(
                available=False,
                hostname="",
                tailnet_name="",
                fqdn="",
                error="Tailscale not connected (no DNSName)",
            )

        # Split "myhost.tailnetname.ts.net" into hostname + tailnet_name
        parts = fqdn.split(".", 1)
        hostname = parts[0]
        tailnet_name = parts[1] if len(parts) > 1 else ""

        tailscale_ips = self_info.get("TailscaleIPs", [])

        logger.debug("Tailscale available: fqdn=%s, ips=%s", fqdn, tailscale_ips)
        return TailscaleStatus(
            available=True,
            hostname=hostname,
            tailnet_name=tailnet_name,
            fqdn=fqdn,
            tailscale_ips=tailscale_ips,
        )


# Module-level singleton — import this in other modules
tailscale_service = TailscaleService()
