"""Small immutable contracts shared by the transport and diagnostic readers."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


@dataclass(frozen=True)
class DiagnosticAccess:
    key_id: int
    owner_id: int
    permissions: frozenset[str]
    is_admin: bool
    printer_ids: frozenset[int] | None
    logs_allowed: bool
    epoch: int

    def permits(self, *permissions: str) -> bool:
        return self.is_admin or all(value in self.permissions for value in permissions)


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class GuideInput(EmptyInput):
    section: Literal["connect", "diagnose", "limits"] | None = None


class Coverage(BaseModel):
    complete: bool = True
    missing: list[str] = Field(default_factory=list)
    truncated: bool = False


class DiagnosticResult(BaseModel):
    contract_version: int = 1
    request_id: str
    generated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    data: dict[str, Any]
    evidence: list[dict[str, Any]] = Field(default_factory=list)
    coverage: Coverage = Field(default_factory=Coverage)
    next_cursor: str | None = None
    warnings: list[str] = Field(default_factory=list)
