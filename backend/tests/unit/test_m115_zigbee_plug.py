"""m115 adds the Zigbee address column.

Asserts against the model table directly, NOT via Base.metadata.create_all:
that needs the entire model graph imported and fails on unrelated foreign keys.
"""

from backend.app.models.smart_plug import SmartPlug


def test_smart_plugs_has_the_zigbee_address():
    col = SmartPlug.__table__.columns["zigbee_ieee"]
    assert col.nullable is True


def test_migration_metadata():
    from backend.app.migrations import m115_zigbee_plug as m

    assert m.version == 115
    assert m.name == "zigbee_plug"
    assert callable(m.upgrade)
