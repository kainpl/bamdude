"""Add ``can_update_energy_cost`` opt-in to ``api_keys``.

API keys have ``SETTINGS_UPDATE`` permission hard-denied via
``_APIKEY_DENIED_PERMISSIONS`` — intentional protection because
``PATCH /settings`` can rewrite SMTP / LDAP / MQTT credentials and the
Home Assistant access token. That's a wider surface than the documented
Home-Assistant-dynamic-tariff use case needs.

This column is a narrowly-scoped opt-in that lets a specific API key
hit ``POST /settings/electricity-price`` (a new, dedicated endpoint
that writes only ``energy_cost_per_kwh``). General ``PATCH /settings``
stays denied for API keys.

Default FALSE so existing keys never silently gain settings-write
capability on upgrade. Upstream Bambuddy #1356 / commit ae29a7dc.
"""

from backend.app.migrations.helpers import add_column

version = 73
name = "api_key_energy_cost_scope"


async def upgrade(conn):
    await add_column(conn, "api_keys", "can_update_energy_cost BOOLEAN DEFAULT 0 NOT NULL")
