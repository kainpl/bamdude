"""The BamDude connector API as the plugin uses it (server contract: spec r2 §7, protocol 1).

Thin and typed: each method is one documented request. Status codes keep their meaning -- 201 a new operation,
200 a replay or a read, 202 work still running -- and every refusal arrives as :class:`HttpError` with the server's
stable code, which is what the plugin branches on (never the translated message).
"""

from __future__ import annotations

from dataclasses import dataclass

from orca_bamdude.transport import HttpError, Transport

PROTOCOL_VERSION = 1


class IncompatibleServer(Exception):
    """The server does not speak this protocol; nothing is sent to it."""


@dataclass(frozen=True)
class Capabilities:
    server_id: str
    storage_epoch: str
    actor_id: str
    server_version: str
    enabled: bool
    features: dict
    limits: dict

    @property
    def namespace(self) -> tuple[str, str, str]:
        """Where this connection's local journal lives: never shared by another server, epoch or key."""
        return self.server_id, self.storage_epoch, self.actor_id

    def allows(self, feature: str) -> bool:
        return bool(self.features.get(feature))


class ConnectorClient:
    def __init__(self, transport: Transport):
        self.transport = transport

    def capabilities(self) -> Capabilities:
        _status, body = self.transport.request_json("GET", "/api/v1/connector/capabilities")
        if not isinstance(body, dict) or body.get("product") != "bamdude":
            raise IncompatibleServer("not_bamdude")
        if body.get("protocol_version") != PROTOCOL_VERSION:
            raise IncompatibleServer("protocol_version")
        try:
            return Capabilities(
                server_id=str(body["connector_server_id"]),
                storage_epoch=str(body["storage_epoch"]),
                actor_id=str(body["actor_id"]),
                server_version=str(body.get("server_version") or ""),
                enabled=bool(body.get("enabled", True)),
                features=dict(body.get("features") or {}),
                limits=dict(body.get("limits") or {}),
            )
        except (KeyError, TypeError) as exc:
            raise IncompatibleServer("capabilities_shape") from exc

    def printers(self) -> list[dict]:
        _status, body = self.transport.request_json("GET", "/api/v1/printers/")
        return [p for p in body or [] if isinstance(p, dict) and p.get("connector_identity")]

    def status_batch(self, printer_ids: list[int]) -> dict[int, dict]:
        if not printer_ids:
            return {}
        _status, body = self.transport.request_json(
            "GET", "/api/v1/printers/status/batch", query=[("ids", pid) for pid in printer_ids[:100]]
        )
        return {int(k): v for k, v in (body or {}).items()}

    def upload(self, file_path: str, filename: str, size: int, *, progress=None, cancelled=None) -> dict:
        _status, body = self.transport.upload_file(
            "/api/v1/connector/files",
            file_path,
            size,
            query={"filename": filename},
            progress=progress,
            cancelled=cancelled,
        )
        return body

    def submit(self, body: dict) -> tuple[int, dict]:
        return self.transport.request_json("POST", "/api/v1/connector/submissions", body=body)

    def _addressed(self, method: str, operation_id: str, suffix: str, server_id: str, epoch: str):
        return self.transport.request_json(
            method,
            f"/api/v1/connector/submissions/{operation_id}{suffix}",
            query={"server_id": server_id, "storage_epoch": epoch},
        )

    def get(self, operation_id: str, server_id: str, epoch: str) -> tuple[int, dict]:
        return self._addressed("GET", operation_id, "", server_id, epoch)

    def resume(self, operation_id: str, server_id: str, epoch: str) -> tuple[int, dict]:
        return self._addressed("POST", operation_id, "/resume", server_id, epoch)

    def cancel(self, operation_id: str, server_id: str, epoch: str) -> tuple[int, dict]:
        return self._addressed("POST", operation_id, "/cancel", server_id, epoch)


__all__ = ["Capabilities", "ConnectorClient", "HttpError", "IncompatibleServer", "PROTOCOL_VERSION"]
