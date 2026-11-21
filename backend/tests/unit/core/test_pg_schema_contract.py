from backend.app.core.pg_schema_contract import compare


def _schema(index):
    return {
        "schema": {
            "sample": {
                "columns": [],
                "pk": {},
                "foreign_keys": [],
                "unique": [],
                "check": [],
                "indexes": [index],
            },
        },
        "extra": {},
    }


def test_only_redundant_postgresql_index_include_metadata_is_ignored():
    index = {
        "name": "sample_uidx",
        "column_names": ["code"],
        "unique": True,
        "dialect_options": {"postgresql_include": ["description"]},
    }
    canonical = _schema(index)

    assert not compare(canonical, _schema({**index, "include_columns": ["description"]}))
    assert compare(canonical, _schema({**index, "include_columns": ["other"]}))
    assert compare(canonical, _schema({**index, "dialect_options": {"postgresql_include": ["other"]}}))
    assert compare(canonical, _schema({**index, "include_columns": None, "dialect_options": None}))
