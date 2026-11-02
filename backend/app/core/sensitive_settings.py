"""One settings privacy policy for support exports and diagnostic log redaction."""

from sqlalchemy import func, or_

# Preserve the support bundle's policy, including PII and infrastructure values.
# Future settings containing these fragments are covered without another list.
SENSITIVE_SETTING_FRAGMENTS = frozenset(
    {
        "access_code",
        "password",
        "token",
        "secret",
        "api_key",
        "installation_id",
        "cloud_token",
        "mqtt_password",
        "email",
        "username",
        "vapid",
        "private_key",
        "public_key",
        "webhook",
        "url",
        "path",
        "config",
        "_ip",
        "host",
        "credential",
        "broker",
        "auth_key",
    }
)


def is_sensitive_setting(key: str) -> bool:
    return any(fragment in key.lower() for fragment in SENSITIVE_SETTING_FRAGMENTS)


def sensitive_settings_predicate(column):
    # Literal substring matching: '_' in api_key/private_key/_ip is not a LIKE
    # wildcard. Filter in SQL so the collector never reads an entire settings dump.
    return or_(
        *(func.lower(column).contains(fragment, autoescape=True) for fragment in sorted(SENSITIVE_SETTING_FRAGMENTS))
    )
