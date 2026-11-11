"""Email service for sending authentication-related emails."""

from __future__ import annotations

import html
import logging
import re
import secrets
import smtplib
import string
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
from backend.app.models.settings import Settings
from backend.app.schemas.auth import MIN_PASSWORD_LENGTH, SMTPSettings

logger = logging.getLogger(__name__)


def generate_secure_password(length: int = 16) -> str:
    """Generate a secure random password.

    Satisfies ``schemas.auth._validate_password_complexity`` by construction —
    one character from each class, then a length no shorter than the floor.

    Args:
        length: Length of the password (default: 16)

    Returns:
        A secure random password containing uppercase, lowercase, digits, and special characters
    """
    if length < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Generated passwords may not be shorter than {MIN_PASSWORD_LENGTH} characters")

    # Define character sets
    lowercase = string.ascii_lowercase
    uppercase = string.ascii_uppercase
    digits = string.digits
    special = "!@#$%^&*()_+-=[]{}|;:,.<>?"

    # Ensure at least one character from each set
    password_chars = [
        secrets.choice(lowercase),
        secrets.choice(uppercase),
        secrets.choice(digits),
        secrets.choice(special),
    ]

    # Fill the rest with random characters from all sets
    all_chars = lowercase + uppercase + digits + special
    password_chars.extend(secrets.choice(all_chars) for _ in range(length - 4))

    # Shuffle to avoid predictable patterns.
    # ⚠️ Not `random.shuffle` — that is the Mersenne Twister, whose state is
    # recoverable from its output. Every character here is drawn with `secrets`,
    # so leaving the ORDER to a predictable generator would be the one
    # non-cryptographic step in the whole function: the first four positions
    # are known to hold one lowercase, one uppercase, one digit and one
    # special, and only the shuffle hides where they went.
    secrets.SystemRandom().shuffle(password_chars)

    return "".join(password_chars)


async def get_notification_template(db: AsyncSession, event_type: str) -> NotificationTemplate | None:
    """Get a notification template by event type from database.

    Args:
        db: Database session
        event_type: Type of event (e.g., 'user_created', 'password_reset')

    Returns:
        NotificationTemplate object or None if not found
    """
    result = await db.execute(select(NotificationTemplate).where(NotificationTemplate.event_type == event_type))
    return result.scalar_one_or_none()


def render_template(template_str: str, variables: dict[str, Any]) -> str:
    """Render a template string with variables.

    Args:
        template_str: Template string with {variable} placeholders
        variables: Dictionary of variables to substitute

    Returns:
        Rendered template string
    """
    result = template_str
    for key, value in variables.items():
        result = result.replace("{" + key + "}", str(value) if value is not None else "")
    # Remove any remaining unreplaced placeholders (case-insensitive, alphanumeric + underscore)
    result = re.sub(r"\{[a-zA-Z0-9_]+\}", "", result)
    return result


async def get_smtp_settings(db: AsyncSession) -> SMTPSettings | None:
    """Get SMTP settings from database.

    Args:
        db: Database session

    Returns:
        SMTPSettings object or None if not configured
    """
    # Fetch all SMTP-related settings
    result = await db.execute(
        select(Settings).where(
            Settings.key.in_(
                [
                    "smtp_host",
                    "smtp_port",
                    "smtp_username",
                    "smtp_password",
                    "smtp_use_tls",
                    "smtp_security",
                    "smtp_auth_enabled",
                    "smtp_from_email",
                    "smtp_from_name",
                ]
            )
        )
    )
    settings_dict = {s.key: s.value for s in result.scalars().all()}

    # Check if minimum required settings are present
    required_keys = ["smtp_host", "smtp_port", "smtp_from_email"]
    if not all(key in settings_dict for key in required_keys):
        return None

    # Handle migration: convert old smtp_use_tls to smtp_security if needed
    smtp_security = settings_dict.get("smtp_security")
    if not smtp_security:
        # Migrate from old smtp_use_tls format
        smtp_use_tls = settings_dict.get("smtp_use_tls", "true").lower() == "true"
        smtp_security = "starttls" if smtp_use_tls else "ssl"

    smtp_auth_enabled = settings_dict.get("smtp_auth_enabled", "true").lower() == "true"

    return SMTPSettings(
        smtp_host=settings_dict["smtp_host"],
        smtp_port=int(settings_dict["smtp_port"]),
        smtp_username=settings_dict.get("smtp_username"),
        smtp_password=settings_dict.get("smtp_password"),
        smtp_security=smtp_security,
        smtp_auth_enabled=smtp_auth_enabled,
        smtp_from_email=settings_dict["smtp_from_email"],
        smtp_from_name=settings_dict.get("smtp_from_name", "BamDude"),
    )


async def save_smtp_settings(db: AsyncSession, smtp_settings: SMTPSettings) -> None:
    """Save SMTP settings to database.

    Args:
        db: Database session
        smtp_settings: SMTP settings to save
    """
    from backend.app.core.db_dialect import upsert_setting

    settings_data = {
        "smtp_host": smtp_settings.smtp_host,
        "smtp_port": str(smtp_settings.smtp_port),
        "smtp_security": smtp_settings.smtp_security,
        "smtp_auth_enabled": "true" if smtp_settings.smtp_auth_enabled else "false",
        "smtp_from_email": smtp_settings.smtp_from_email,
        "smtp_from_name": smtp_settings.smtp_from_name,
    }

    # The route has already resolved keep/clear/replace under the credential
    # anchor. Persist the exact resolved snapshot, including deliberate clears.
    settings_data["smtp_username"] = smtp_settings.smtp_username or ""
    settings_data["smtp_password"] = smtp_settings.smtp_password or ""

    # ⚠️ Through upsert_setting, never a hand-rolled ON CONFLICT: building the
    # statement with dialects.sqlite.insert on a PostgreSQL install hands the PG
    # compiler the wrong OnConflictDoUpdate and raises. See set_setup_completed
    # in routes/auth.py for what that cost.
    for key, value in settings_data.items():
        await upsert_setting(db, Settings, key, value)


def send_email(
    smtp_settings: SMTPSettings,
    to_email: str,
    subject: str,
    body_text: str,
    body_html: str | None = None,
) -> None:
    """Send an email using SMTP.

    Args:
        smtp_settings: SMTP configuration
        to_email: Recipient email address
        subject: Email subject
        body_text: Plain text body
        body_html: Optional HTML body

    Raises:
        Exception: If email sending fails
    """
    msg = MIMEMultipart("alternative")
    msg["From"] = f"{smtp_settings.smtp_from_name} <{smtp_settings.smtp_from_email}>"
    msg["To"] = to_email
    msg["Subject"] = subject

    # Attach plain text part
    msg.attach(MIMEText(body_text, "plain"))

    # Attach HTML part if provided
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    # Send email
    try:
        security = smtp_settings.smtp_security
        auth_enabled = smtp_settings.smtp_auth_enabled

        # Validate username is provided when authentication is enabled
        if auth_enabled and smtp_settings.smtp_password:
            if not smtp_settings.smtp_username:
                raise ValueError("SMTP username is required when authentication is enabled")

        if security == "ssl":
            # Direct SSL connection (typically port 465)
            with smtplib.SMTP_SSL(smtp_settings.smtp_host, smtp_settings.smtp_port, timeout=10) as server:
                if auth_enabled and smtp_settings.smtp_password and smtp_settings.smtp_username:
                    server.login(smtp_settings.smtp_username, smtp_settings.smtp_password)
                server.send_message(msg)
        elif security == "starttls":
            # STARTTLS upgrade (typically port 587)
            with smtplib.SMTP(smtp_settings.smtp_host, smtp_settings.smtp_port, timeout=10) as server:
                server.starttls()
                if auth_enabled and smtp_settings.smtp_password and smtp_settings.smtp_username:
                    server.login(smtp_settings.smtp_username, smtp_settings.smtp_password)
                server.send_message(msg)
        else:
            # No encryption (typically port 25) - use with caution
            with smtplib.SMTP(smtp_settings.smtp_host, smtp_settings.smtp_port, timeout=10) as server:
                if auth_enabled and smtp_settings.smtp_password and smtp_settings.smtp_username:
                    server.login(smtp_settings.smtp_username, smtp_settings.smtp_password)
                server.send_message(msg)
        logger.info(f"Email sent successfully to {to_email}")
    except Exception as e:
        logger.error(f"Failed to send email to {to_email}: {e}")
        raise


def _plain_field(value: str) -> str:
    """A credential field is one displayed line, never mail markup/headers."""
    return "".join(f"\\u{ord(char):04x}" if ord(char) < 32 or char in "\u2028\u2029" else char for char in value)


def _credential_email(
    title_template: str,
    body_template: str,
    *,
    username: str,
    url: str,
    password: str | None = None,
    ttl_hours: int | None = None,
    app_name: str = "BamDude",
    admin_reset: bool = False,
) -> tuple[str, str, str]:
    """Render public copy first, then append the code-owned credential block.

    Neither password nor any login/reset URL is given to the editable title or
    body. Even a URL-wrapped placeholder therefore cannot transmit a credential
    to the template author's destination. Three separate event meanings remain.
    """
    from urllib.parse import urlsplit

    from backend.app.i18n import current_language

    # The URL is supplied by the server, not the editable template. Still
    # reject active schemes/control chars before putting it into an HTML link.
    try:
        parsed = urlsplit(url)
        valid = (
            parsed.scheme in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
        )
    except ValueError:
        valid = False
    if not valid or any(char.isspace() or ord(char) < 32 for char in url):
        raise ValueError("Invalid authentication email URL")

    public = {"app_name": app_name, "username": _plain_field(username), "ttl_hours": ttl_hours}
    subject = " ".join(render_template(title_template, public).splitlines())
    editable = render_template(body_template, public)
    uk = current_language() == "uk"
    user_label = "Користувач" if uk else "Username"
    password_label = ("Новий пароль" if uk else "New Password") if admin_reset else ("Пароль" if uk else "Password")
    link_label = (
        ("Скинути пароль" if uk else "Reset Password") if password is None else ("Увійти" if uk else "Login Now")
    )
    warning = (
        "Якщо ви не запитували скидання пароля, негайно зверніться до адміністратора."
        if uk
        else "If you did not request this password reset, please contact your administrator immediately."
    )
    security = (
        "Після входу змініть пароль." if uk else "For security reasons, please change your password after logging in."
    )
    fields = [(user_label, _plain_field(username))]
    if password is not None:
        fields.append((password_label, _plain_field(password)))
    text_block = "\n".join(f"{label}: {value}" for label, value in fields)
    text_block += f"\n{link_label}: {url}"
    if ttl_hours is not None:
        text_block += f"\n{'Посилання дійсне, год' if uk else 'Link expires in hours'}: {ttl_hours}"
    if password is not None:
        text_block += "\n" + security
    if admin_reset or password is None:
        text_block += "\n" + warning
    text_body = editable + "\n\n" + text_block

    escaped_body = html.escape(editable).replace("\n", "<br>\n")
    field_html = "".join(
        f"<p><strong>{html.escape(label)}:</strong> <code>{html.escape(value)}</code></p>" for label, value in fields
    )
    alert_label = "Попередження безпеки" if uk else "Security Alert"
    alert = f"<p><strong>{alert_label}:</strong> {html.escape(warning)}</p>" if admin_reset or password is None else ""
    security_html = f"<p>{html.escape(security)}</p>" if password is not None else ""
    ttl_html = (
        f"<p>{'Посилання дійсне, год' if uk else 'Link expires in hours'}: {ttl_hours}</p>"
        if ttl_hours is not None
        else ""
    )
    html_body = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1.0"></head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background-color: #667eea; padding: 30px; border-radius: 8px 8px 0 0;">
        <h1 style="color: #ffffff;">{html.escape(subject)}</h1>
    </div>
    <div style="background: #f9f9f9; padding: 30px; border-radius: 0 0 8px 8px; border: 1px solid #ddd;">
        <div style="font-size: 16px;">{escaped_body}</div>
        <div data-auth-credentials="true">{field_html}
            <p><a href="{html.escape(url, quote=True)}">{link_label}</a></p>
            {ttl_html}{security_html}{alert}
        </div>
    </div>
</body></html>
"""
    return subject, text_body, html_body


def create_welcome_email(username: str, password: str, login_url: str) -> tuple[str, str, str]:
    """Built-in welcome copy, with the same protected block as custom copy."""
    return _credential_email(
        "Welcome to BamDude - Your Account Details",
        "Welcome to BamDude!\n\nYour account has been created. Here are your login details:",
        username=username,
        password=password,
        url=login_url,
    )


def create_password_reset_email(username: str, password: str, login_url: str) -> tuple[str, str, str]:
    """Admin reset mails a generated password, not the self-service link."""
    return _credential_email(
        "BamDude - Your Password Has Been Reset",
        "Your BamDude password has been reset.\n\nYour login details:",
        username=username,
        password=password,
        url=login_url,
        admin_reset=True,
    )


async def create_welcome_email_from_template(
    db: AsyncSession, username: str, password: str, login_url: str, app_name: str = "BamDude"
) -> tuple[str, str, str]:
    template = await get_notification_template(db, "user_created")
    if template is None:
        logger.warning("No welcome email template found in database, using default")
        return create_welcome_email(username, password, login_url)
    return _credential_email(
        template.title_template,
        template.body_template,
        username=username,
        password=password,
        url=login_url,
        app_name=app_name,
    )


async def create_password_reset_email_from_template(
    db: AsyncSession, username: str, password: str, login_url: str, app_name: str = "BamDude"
) -> tuple[str, str, str]:
    template = await get_notification_template(db, "password_reset")
    if template is None:
        logger.warning("No password reset email template found in database, using default")
        return create_password_reset_email(username, password, login_url)
    return _credential_email(
        template.title_template,
        template.body_template,
        username=username,
        password=password,
        url=login_url,
        app_name=app_name,
        admin_reset=True,
    )


async def create_password_reset_link_email_from_template(
    db: AsyncSession, username: str, reset_url: str, ttl_hours: int, app_name: str = "BamDude"
) -> tuple[str, str, str]:
    """Self-service reset remains a one-time link, never a generated password."""
    template = await get_notification_template(db, "password_reset_link")
    if template is None:
        logger.warning("No password_reset_link template in the database, using the built-in copy")
        template = next(x for x in DEFAULT_TEMPLATES if x["event_type"] == "password_reset_link")
        title, body = template["title_template"], template["body_template"]
    else:
        title, body = template.title_template, template.body_template
    return _credential_email(
        title,
        body,
        username=username,
        url=reset_url,
        ttl_hours=ttl_hours,
        app_name=app_name,
    )


async def send_user_print_notification(
    db: AsyncSession,
    event_type: str,
    user_email: str,
    username: str,
    variables: dict,
) -> None:
    """Send a print notification email to a user using Advanced Auth SMTP settings.

    Args:
        db: Database session
        event_type: One of 'user_print_start', 'user_print_complete', 'user_print_failed', 'user_print_stopped'
        user_email: Recipient email address
        username: Username of the recipient
        variables: Template variables (printer, filename, etc.)
    """
    # Check that advanced auth is enabled (SMTP settings must be configured)
    smtp_settings = await get_smtp_settings(db)
    if not smtp_settings:
        logger.warning("Cannot send user print notification: SMTP settings not configured")
        return

    # Get the template
    template = await get_notification_template(db, event_type)
    if template is None:
        logger.warning("No template found for event type: %s", event_type)
        return

    # Add common variables (username, timestamp, app_name) merged with caller-supplied variables
    all_variables = {
        "username": username,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "app_name": "BamDude",
        **variables,
    }

    subject = render_template(template.title_template, all_variables)
    text_body = render_template(template.body_template, all_variables)

    # Build HTML body - content comes entirely from the database template
    escaped_text_body = html.escape(text_body).replace("\n", "<br>\n")
    html_body = f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
</head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333; max-width: 600px; margin: 0 auto; padding: 20px;">
    <div style="background: linear-gradient(135deg, #1db954 0%, #158a3e 100%); background-color: #1db954; padding: 20px; border-radius: 8px 8px 0 0;">
        <h1 style="color: #ffffff; margin: 0; font-size: 24px; text-shadow: 0 1px 2px rgba(0,0,0,0.3);">{html.escape(subject)}</h1>
    </div>
    <div style="background: #f9f9f9; padding: 30px; border-radius: 0 0 8px 8px; border: 1px solid #ddd; border-top: none;">
        <div style="font-size: 16px;">{escaped_text_body}</div>
    </div>
</body>
</html>
"""

    try:
        send_email(smtp_settings, user_email, subject, text_body, html_body)
        logger.info("Sent %s notification email to %s", event_type, user_email)
    except Exception as e:
        logger.error("Failed to send %s notification to %s: %s", event_type, user_email, e)
