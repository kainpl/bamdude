from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator

RecoveryKind = Literal["item", "header", "archive", "runtime", "episode", "repair"]

RecoveryAction = Literal[
    "align_terminal_item",
    "fill_estimated_end_and_align",
    "release_orphan_header",
    "delete_stale_item",
    "close_interrupted_attempt",
    "clear_terminal_runtime_context",
    "cancel_terminal_claims",
    "settle_swap_by_operator",
    "restore_gate_owner_row",
]


class RecoveryPreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    #: ``runtime`` targets a printer's held run by its execution archive id.
    kind: Literal["item", "header", "runtime", "archive"]
    target_id: int = Field(gt=0, le=2_147_483_647)
    action: RecoveryAction
    confirmed_completed: StrictBool = False
    confirmed_idle: StrictBool = False
    confirmed_manual_swap: StrictBool = False
    note: str = Field(default="", max_length=500)


class RecoveryApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_token: str = Field(min_length=32, max_length=8192)
    confirmed: Literal[True]

    @field_validator("confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value):
        if value is not True:
            raise ValueError("explicit confirmation required")
        return value
