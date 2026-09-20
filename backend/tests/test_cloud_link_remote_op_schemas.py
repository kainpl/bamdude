import pytest
from pydantic import ValidationError

from backend.app.services.cloud_link.remote_ops_schemas import (
    EditSpoolArgs,
    ListSpoolsArgs,
)


def test_list_spools_args_defaults_include_archived_false():
    assert ListSpoolsArgs.model_validate({}).include_archived is False


def test_edit_spool_args_shape():
    a = EditSpoolArgs.model_validate({"spool_id": 1, "patch": {"note": "x"}})
    assert a.spool_id == 1 and a.patch == {"note": "x"}


def test_edit_spool_args_reject_extra_key():
    with pytest.raises(ValidationError):
        EditSpoolArgs.model_validate({"spool_id": 1, "patch": {}, "sneaky": 1})
