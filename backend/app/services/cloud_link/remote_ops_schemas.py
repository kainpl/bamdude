from pydantic import BaseModel, ConfigDict, Field


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")  # a remote op's args are a closed shape


class ListSpoolsArgs(_Args):
    include_archived: bool = False
    # Pagination is part of the op from day one: a real inventory dumped whole
    # weighed ~700 KB in one cmd_result — past the portal's 512 KiB ws kill
    # line. The cap keeps any single page comfortably inside one frame; the
    # portal reads ``total`` off the result to page further.
    limit: int = Field(default=500, ge=1, le=1000)
    offset: int = Field(default=0, ge=0)


class EditSpoolArgs(_Args):
    spool_id: int
    patch: dict
