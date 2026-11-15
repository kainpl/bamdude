"""The two stock alerts name the filament, not only its material (upstream 318430f0f).

The forecast groups by colour, but the shipped texts said "{material} ({brand})":
Black and White of one product sent the same sentence. The sender now offers
``{filament}`` — material, subtype and colour, a colour that only repeats the
subtype named once — and the default texts use it.

There is one row per event, not per language, and an operator may have edited
either column: a title or a body is replaced only where it still equals a text
this project shipped (English, the model's seed — the same text — or Ukrainian),
each column on its own. The old and new texts are frozen here; nothing is
imported from the templates of a later release.
"""

from sqlalchemy import Text, column, table, update

version = 229
name = "stock_alert_names_the_colour"

_TEMPLATES = table(
    "notification_templates",
    column("event_type", Text),
    column("title_template", Text),
    column("body_template", Text),
)

# (event, column, previously shipped text, replacement)
_REPLACEMENTS = (
    ("stock_reorder_alert", "title_template", "Reorder Alert: {material}", "Reorder Alert: {filament}"),
    ("stock_reorder_alert", "title_template", "Час замовляти: {material}", "Час замовляти: {filament}"),
    (
        "stock_reorder_alert",
        "body_template",
        "{material} ({brand}) has reached the reorder point.\n"
        "Stock: {stock_g}g | Rate: {rate_g_day}g/day | Days left: {days_left}d\n"
        "Reorder now to avoid a stock break.",
        "{filament} ({brand}) has reached the reorder point.\n"
        "Stock: {stock_g}g | Rate: {rate_g_day}g/day | Days left: {days_left}d\n"
        "Reorder now to avoid a stock break.",
    ),
    (
        "stock_reorder_alert",
        "body_template",
        "{material} ({brand}) досяг точки повторного замовлення.\n"
        "Залишок: {stock_g}г | Темп: {rate_g_day}г/день | Днів лишилось: {days_left}\n"
        "Замовляй зараз, щоб не залишитися без матеріалу.",
        "{filament} ({brand}) досяг точки повторного замовлення.\n"
        "Залишок: {stock_g}г | Темп: {rate_g_day}г/день | Днів лишилось: {days_left}\n"
        "Замовляй зараз, щоб не залишитися без матеріалу.",
    ),
    ("stock_break_alert", "title_template", "Stock Break Risk: {material}", "Stock Break Risk: {filament}"),
    ("stock_break_alert", "title_template", "Ризик закінчення: {material}", "Ризик закінчення: {filament}"),
    (
        "stock_break_alert",
        "body_template",
        "{material} ({brand}) will run out before replenishment arrives.\n"
        "Stock: {stock_g}g | Rate: {rate_g_day}g/day | Lead time: {lead_time_days}d\n"
        "Only {days_left}d of stock remaining — order immediately.",
        "{filament} ({brand}) will run out before replenishment arrives.\n"
        "Stock: {stock_g}g | Rate: {rate_g_day}g/day | Lead time: {lead_time_days}d\n"
        "Only {days_left}d of stock remaining — order immediately.",
    ),
    (
        "stock_break_alert",
        "body_template",
        "{material} ({brand}) закінчиться до прибуття поповнення.\n"
        "Залишок: {stock_g}г | Темп: {rate_g_day}г/день | Термін постачання: {lead_time_days}д\n"
        "Лишилось лише {days_left}д запасу — замовляй негайно.",
        "{filament} ({brand}) закінчиться до прибуття поповнення.\n"
        "Залишок: {stock_g}г | Темп: {rate_g_day}г/день | Термін постачання: {lead_time_days}д\n"
        "Лишилось лише {days_left}д запасу — замовляй негайно.",
    ),
)


async def upgrade(conn):
    for event, field, old, new in _REPLACEMENTS:
        await conn.execute(
            update(_TEMPLATES).where(_TEMPLATES.c.event_type == event, _TEMPLATES.c[field] == old).values({field: new})
        )
