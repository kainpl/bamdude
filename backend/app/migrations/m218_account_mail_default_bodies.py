"""Refresh only unchanged pre-G01 account-mail bodies after credential-block isolation.

The sender now appends credentials outside editable templates. Historical
defaults otherwise leave empty Password/Login labels on upgraded databases.
Freeze both localized bodies and the model-seeded English reset-link variant
from ecf1c4bcc; do not import future templates, ORM defaults or runtime policy.
There is one row per event, not per language: match its stored text, regardless
of the installation's current language or its is_default flag. A customized
body, title, name and metadata remain untouched.
"""

from sqlalchemy import Text, column, table, update

version = 218
name = "account_mail_default_bodies"

_TEMPLATES = table("notification_templates", column("event_type", Text), column("body_template", Text))

# (event, previously shipped body, replacement). These are historical values,
# including the model's ASCII hyphen versus the EN locale file's em dash.
_BODY_REPLACEMENTS = (
    (
        "user_created",
        "Welcome {username}!\n\nYour account has been created.\n"
        "Username: {username}\nPassword: {password}\n\nLogin at: {login_url}",
        "Welcome {username}!\n\nYour account has been created. Your sign-in details are added securely below.",
    ),
    (
        "user_created",
        "Вітаємо, {username}!\n\nВаш обліковий запис створено.\n"
        "Ім'я користувача: {username}\nПароль: {password}\n\nУвійти: {login_url}",
        "Вітаємо, {username}!\n\nВаш обліковий запис створено. Дані для входу безпечно додано нижче.",
    ),
    (
        "password_reset",
        "Hello {username},\n\nYour password has been reset.\nNew Password: {password}\n\nLogin at: {login_url}",
        "Hello {username},\n\nYour password has been reset. Your new sign-in details are added securely below.",
    ),
    (
        "password_reset",
        "Привіт, {username}!\n\nВаш пароль було скинуто.\nНовий пароль: {password}\n\nУвійти: {login_url}",
        "Привіт, {username}!\n\nВаш пароль було скинуто. Нові дані для входу безпечно додано нижче.",
    ),
    (
        "password_reset_link",
        "Hello {username},\n\nSomebody asked to reset the password for your {app_name} account. "
        "Open the link below to choose a new one:\n\n{reset_url}\n\n"
        "The link works once and stops working after {ttl_hours} hour(s).\n\n"
        "If this was not you, ignore this message - your password has not been changed.",
        "Hello {username},\n\nSomebody asked to reset the password for your {app_name} account. "
        "Use the protected reset link at the end of this email to choose a new password.\n\n"
        "The link works once and stops working after {ttl_hours} hour(s).\n\n"
        "If this was not you, ignore this message - your password has not been changed.",
    ),
    (
        "password_reset_link",
        "Hello {username},\n\nSomebody asked to reset the password for your {app_name} account. "
        "Open the link below to choose a new one:\n\n{reset_url}\n\n"
        "The link works once and stops working after {ttl_hours} hour(s).\n\n"
        "If this was not you, ignore this message — your password has not been changed.",
        "Hello {username},\n\nSomebody asked to reset the password for your {app_name} account. "
        "Use the protected reset link at the end of this email to choose a new password.\n\n"
        "The link works once and stops working after {ttl_hours} hour(s).\n\n"
        "If this was not you, ignore this message — your password has not been changed.",
    ),
    (
        "password_reset_link",
        "Вітаємо, {username}!\n\nХтось попросив скинути пароль для вашого облікового запису {app_name}. "
        "Відкрийте посилання нижче, щоб задати новий:\n\n{reset_url}\n\n"
        "Посилання спрацює один раз і перестає діяти через {ttl_hours} год.\n\n"
        "Якщо це були не ви — просто знехтуйте цим листом: пароль не змінився.",
        "Вітаємо, {username}!\n\nХтось попросив скинути пароль для вашого облікового запису {app_name}. "
        "Щоб задати новий пароль, скористайтеся захищеним посиланням наприкінці цього листа.\n\n"
        "Посилання спрацює один раз і перестає діяти через {ttl_hours} год.\n\n"
        "Якщо це були не ви — просто знехтуйте цим листом: пароль не змінився.",
    ),
)


async def upgrade(conn):
    for event, old_body, new_body in _BODY_REPLACEMENTS:
        await conn.execute(
            update(_TEMPLATES)
            .where(_TEMPLATES.c.event_type == event, _TEMPLATES.c.body_template == old_body)
            .values(body_template=new_body)
        )
