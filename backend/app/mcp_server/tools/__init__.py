"""Explicit implemented tools, never placeholders for future readers."""

from dataclasses import dataclass

from pydantic import BaseModel


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_model: type[BaseModel]
