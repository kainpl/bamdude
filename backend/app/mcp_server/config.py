"""Validated, persisted MCP configuration; never inferred from request headers."""

import ipaddress
import json
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select


def exact_host(value: str) -> str:
    if not value or len(value) > 255 or any(char in value for char in "/\\@?#*\r\n\t "):
        raise ValueError("MCP hosts must be exact host:port values")
    parsed = urlsplit("//" + value)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Invalid MCP host port") from None
    if not parsed.hostname or (port is not None and not 1 <= port <= 65535):
        raise ValueError("Invalid MCP host")
    return value.lower()


def is_loopback_host(value: str) -> bool:
    try:
        exact_host(value)
        hostname = urlsplit("//" + value).hostname
        return hostname == "localhost" or ipaddress.ip_address(hostname).is_loopback
    except ValueError:
        return False


def canonical_endpoint(value: str) -> str:
    """An operator-supplied display/config address, never request-derived."""
    if not value:
        return ""
    parsed = urlsplit(value)
    if (
        len(value) > 2048
        or parsed.scheme not in {"http", "https"}
        or parsed.path != "/mcp"
        or parsed.username is not None
        or parsed.password is not None
        or "?" in value
        or "#" in value
    ):
        raise ValueError("Invalid MCP endpoint")
    host = exact_host(parsed.netloc)
    if parsed.scheme == "http" and not is_loopback_host(host):
        raise ValueError("MCP requires HTTPS outside loopback")
    return f"{parsed.scheme}://{host}/mcp"


class McpConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool = False
    allowed_hosts: list[str] = Field(default_factory=list, max_length=32)
    allowed_origins: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("allowed_hosts")
    @classmethod
    def hosts(cls, values):
        return list(dict.fromkeys(exact_host(value) for value in values))

    @field_validator("allowed_origins")
    @classmethod
    def origins(cls, values):
        for value in values:
            parsed = urlsplit(value)
            if (
                len(value) > 255
                or parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path
                or parsed.query
                or parsed.fragment
                or parsed.username
                or parsed.password
            ):
                raise ValueError("MCP origins must be exact http(s) origins")
            exact_host(parsed.netloc)
        return list(dict.fromkeys(values))


async def read_config() -> McpConfig:
    from backend.app.core import database
    from backend.app.models.settings import Settings

    async with database.async_session() as db:
        rows = (
            await db.execute(
                select(Settings.key, Settings.value).where(
                    Settings.key.in_(["mcp_enabled", "mcp_allowed_hosts", "mcp_allowed_origins"])
                )
            )
        ).all()
    values = dict(rows)
    return McpConfig(
        enabled=values.get("mcp_enabled") == "true",
        allowed_hosts=json.loads(values.get("mcp_allowed_hosts", "[]")),
        allowed_origins=json.loads(values.get("mcp_allowed_origins", "[]")),
    )
