from pydantic import BaseModel, ConfigDict


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a remote op's args are a closed shape


class ListSpoolsArgs(_Args):
    include_archived: bool = False


class EditSpoolArgs(_Args):
    spool_id: int
    patch: dict
