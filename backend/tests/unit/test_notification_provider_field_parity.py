"""Every event toggle of the registry can be created AND updated.

The update route folds only the keys the update schema carries into the stored
subscription, so a toggle present in the registry and absent from that schema
would answer 200 and write nothing — upstream's #2945, where two stock alert
toggles never reached the database.
"""

from backend.app.models.notification import PROVIDER_EVENT_DEFAULTS
from backend.app.schemas.notification import NotificationProviderBase, NotificationProviderUpdate


def toggles(schema) -> set[str]:
    return {name for name in schema.model_fields if name.startswith("on_")}


def test_the_create_schema_carries_every_registry_toggle_and_no_other():
    assert toggles(NotificationProviderBase) == set(PROVIDER_EVENT_DEFAULTS)


def test_the_update_schema_carries_every_registry_toggle_and_no_other():
    assert toggles(NotificationProviderUpdate) == set(PROVIDER_EVENT_DEFAULTS)


def test_a_new_provider_starts_with_the_registry_defaults():
    defaults = {name: NotificationProviderBase.model_fields[name].default for name in toggles(NotificationProviderBase)}
    assert defaults == PROVIDER_EVENT_DEFAULTS


def test_an_update_leaves_every_toggle_unset_unless_named():
    assert all(
        NotificationProviderUpdate.model_fields[name].default is None for name in toggles(NotificationProviderUpdate)
    )
