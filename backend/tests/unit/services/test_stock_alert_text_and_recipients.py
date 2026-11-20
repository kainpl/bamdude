"""Stock alerts: which filament, who hears it, and the legacy settings row
(upstream 318430f0f legs 3, 4, 9 — G05 U22).

* The forecast groups by colour, but the message named only material and brand:
  Black and White of one product sent the same sentence twice.
* A recipient subscribed to "reorder" alone heard nothing about a SKU that was
  already in "break" — break and reorder are exclusive per SKU.
* The tick's stamp row for a coloured SKU carried lead time 0 and shadowed the
  colourless settings row the engine had fallen back to: a 10-day lead time
  became 0 six hours later.
"""

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from backend.app.migrations import m229_stock_alert_names_the_colour as migration
from backend.app.models.filament_sku_settings import FilamentSkuSettings
from backend.app.models.notification import NotificationProvider
from backend.app.models.notification_template import DEFAULT_TEMPLATES, NotificationTemplate
from backend.app.models.spool import Spool
from backend.app.models.spool_usage_history import SpoolUsageHistory
from backend.app.models.user import User
from backend.app.models.user_notification import UserNotification
from backend.app.services.notification_service import NotificationService
from backend.app.services.stock_forecast_alerts import StockForecastAlerts, find_stock_alerts

_DATA = Path(__file__).resolve().parents[3] / "app/data"
_EVENTS = ("stock_reorder_alert", "stock_break_alert", "stock_depleted_alert")

# The bodies and titles shipped before this change — frozen here, never imported.
_OLD = {
    "en": {
        "stock_reorder_alert": (
            "Reorder Alert: {material}",
            "{material} ({brand}) has reached the reorder point.\n"
            "Stock: {stock_g}g | Rate: {rate_g_day}g/day | Days left: {days_left}d\n"
            "Reorder now to avoid a stock break.",
        ),
        "stock_break_alert": (
            "Stock Break Risk: {material}",
            "{material} ({brand}) will run out before replenishment arrives.\n"
            "Stock: {stock_g}g | Rate: {rate_g_day}g/day | Lead time: {lead_time_days}d\n"
            "Only {days_left}d of stock remaining — order immediately.",
        ),
    },
    "uk": {
        "stock_reorder_alert": (
            "Час замовляти: {material}",
            "{material} ({brand}) досяг точки повторного замовлення.\n"
            "Залишок: {stock_g}г | Темп: {rate_g_day}г/день | Днів лишилось: {days_left}\n"
            "Замовляй зараз, щоб не залишитися без матеріалу.",
        ),
        "stock_break_alert": (
            "Ризик закінчення: {material}",
            "{material} ({brand}) закінчиться до прибуття поповнення.\n"
            "Залишок: {stock_g}г | Темп: {rate_g_day}г/день | Термін постачання: {lead_time_days}д\n"
            "Лишилось лише {days_left}д запасу — замовляй негайно.",
        ),
    },
}


def _shipped(lang: str) -> dict:
    return json.loads((_DATA / f"notification_templates_{lang}.json").read_text(encoding="utf-8"))


async def _seed_templates(db, lang: str) -> None:
    shipped = _shipped(lang)
    for event in _EVENTS:
        db.add(
            NotificationTemplate(
                event_type=event,
                name=shipped[event]["name"],
                title_template=shipped[event]["title_template"],
                body_template=shipped[event]["body_template"],
            )
        )
    await db.commit()


async def _rendered(db, call) -> tuple[str, str]:
    """(title, body) the real service would send for one alert."""
    service = NotificationService()
    sent = AsyncMock()
    with (
        patch.object(service, "_get_providers_for_event", AsyncMock(return_value=[SimpleNamespace()])),
        patch.object(service, "_send_to_providers", sent),
    ):
        await call(service)
    return sent.await_args.args[1], sent.await_args.args[2]


def _reorder(**fields):
    base = {"material": "PLA", "brand": "Bambu Lab", "stock_g": 420.0, "rate_g_day": 30.0, "days_left": 14}
    return lambda service, db: service.on_stock_reorder_alert(**{**base, **fields}, db=db)


class TestTheMessageNamesTheColour:
    @pytest.mark.asyncio
    @pytest.mark.parametrize(("lang", "point"), [("en", "has reached the reorder point"), ("uk", "досяг точки")])
    async def test_two_colours_of_one_product_read_differently(self, db_session, lang, point) -> None:
        await _seed_templates(db_session, lang)

        black = await _rendered(db_session, lambda s: _reorder(subtype="Basic", color="Black")(s, db_session))
        white = await _rendered(db_session, lambda s: _reorder(subtype="Basic", color="White")(s, db_session))

        assert "PLA Basic Black" in black[0] and "PLA Basic Black (Bambu Lab) " + point in black[1]
        assert "PLA Basic White" in white[0] and "PLA Basic White" in white[1]
        assert black != white

    @pytest.mark.asyncio
    async def test_the_break_message_names_it_too(self, db_session) -> None:
        await _seed_templates(db_session, "en")

        title, body = await _rendered(
            db_session,
            lambda s: s.on_stock_break_alert(
                material="PETG",
                subtype="HF",
                color="Orange",
                brand="Bambu Lab",
                stock_g=240.0,
                rate_g_day=30.0,
                days_left=8,
                lead_time_days=10,
                db=db_session,
            ),
        )

        assert title == "Stock Break Risk: PETG HF Orange"
        assert body.startswith("PETG HF Orange (Bambu Lab) will run out")
        assert "Lead time: 10d" in body

    @pytest.mark.asyncio
    async def test_a_colour_that_only_repeats_the_subtype_is_named_once(self, db_session) -> None:
        """Spoolman has no colour name: the subtype stands in for it."""
        await _seed_templates(db_session, "en")

        title, body = await _rendered(
            db_session, lambda s: _reorder(subtype="Basic Black", color=" basic black ")(s, db_session)
        )

        assert title == "Reorder Alert: PLA Basic Black"
        assert "Basic Black Basic Black" not in body

    @pytest.mark.asyncio
    async def test_a_sku_without_subtype_or_colour_reads_as_before(self, db_session) -> None:
        await _seed_templates(db_session, "en")

        title, body = await _rendered(db_session, lambda s: _reorder()(s, db_session))

        assert title == "Reorder Alert: PLA"
        assert body.startswith("PLA (Bambu Lab) has reached the reorder point.")

    @pytest.mark.asyncio
    async def test_the_tick_hands_over_subtype_and_colour(self, db_session, monkeypatch) -> None:
        await _sku(db_session, color_name="Black", subtype="Basic", remaining=350.0)
        sent = AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        db_session.add(_setting("forecast_global_lead_time_days", "7"))
        await db_session.commit()
        with (
            patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", sent),
            patch("backend.app.services.notification_service.notification_service.on_stock_reorder_alert", AsyncMock()),
        ):
            await StockForecastAlerts().tick()

        assert (sent.await_args.kwargs["subtype"], sent.await_args.kwargs["color"]) == ("Basic", "Black")


class TestTheShippedTextsAreUpgraded:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("lang", ["en", "uk"])
    async def test_an_untouched_default_becomes_the_new_one(self, db_session, lang) -> None:
        for event in _OLD[lang]:  # the two texts m229 upgrades
            title, body = _OLD[lang][event]
            db_session.add(NotificationTemplate(event_type=event, name="n", title_template=title, body_template=body))
        await db_session.commit()

        await migration.upgrade(await db_session.connection())
        await db_session.commit()

        shipped = _shipped(lang)
        rows = (await db_session.execute(select(NotificationTemplate))).scalars().all()
        for row in rows:
            await db_session.refresh(row)
            assert row.title_template == shipped[row.event_type]["title_template"]
            assert row.body_template == shipped[row.event_type]["body_template"]
            assert "{filament}" in row.title_template and "{filament}" in row.body_template

    @pytest.mark.asyncio
    async def test_an_edited_text_is_left_alone(self, db_session) -> None:
        old_title, _ = _OLD["en"]["stock_reorder_alert"]
        db_session.add(
            NotificationTemplate(
                event_type="stock_reorder_alert", name="n", title_template=old_title, body_template="Buy {material}!"
            )
        )
        await db_session.commit()

        await migration.upgrade(await db_session.connection())
        await db_session.commit()

        row = (await db_session.execute(select(NotificationTemplate))).scalars().one()
        await db_session.refresh(row)
        assert row.body_template == "Buy {material}!"  # the operator's own
        assert row.title_template == "Reorder Alert: {filament}"  # still the default, so upgraded

    def test_the_seed_and_the_english_file_agree(self) -> None:
        seeded = {row["event_type"]: row for row in DEFAULT_TEMPLATES}
        for event in _EVENTS:
            assert seeded[event]["title_template"] == _shipped("en")[event]["title_template"]
            assert seeded[event]["body_template"] == _shipped("en")[event]["body_template"]


class TestAReorderOnlyRecipientHearsAboutABreak:
    """Break is "reorder, urgently": silence is the worst answer for someone who
    asked to be told when to order (G05 Р31)."""

    @staticmethod
    def _provider(name: str, events: list[str]) -> NotificationProvider:
        return NotificationProvider(
            name=name,
            provider_type="ntfy",
            enabled=True,
            config=json.dumps({"topic": name}),
            subscribed_events=events,
            quiet_hours_enabled=False,
            daily_digest_enabled=False,
        )

    @pytest.mark.asyncio
    async def test_each_recipient_gets_one_message_of_the_kind_it_asked_for(self, db_session, monkeypatch) -> None:
        from backend.app.services.notification_service import notification_service

        await _seed_templates(db_session, "en")
        db_session.add_all(
            [
                self._provider("reorder-only", ["on_stock_reorder_alert"]),
                self._provider("both", ["on_stock_reorder_alert", "on_stock_break_alert"]),
                self._provider("break-only", ["on_stock_break_alert"]),
                self._provider("neither", ["on_print_complete"]),
                User(username="u-reorder", password_hash="x", role="user", inbox_events=["stock_reorder_alert"]),
                User(
                    username="u-both",
                    password_hash="x",
                    role="user",
                    inbox_events=["stock_reorder_alert", "stock_break_alert"],
                ),
                _setting("forecast_global_lead_time_days", "7"),
            ]
        )
        await db_session.commit()
        await _sku(db_session, color_name="Black", subtype="Basic", remaining=350.0)  # in break

        heard: list[tuple[str, str, object]] = []

        async def capture(provider, title, message, db=None, **kwargs):
            heard.append((provider.name, kwargs["event_type"], kwargs.get("chat_filter")))
            return True, ""

        notification_service._template_cache.clear()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch.object(notification_service, "_send_to_provider", capture),
            patch.object(notification_service, "_update_provider_status", AsyncMock()),
            patch.object(notification_service, "_log_notification", AsyncMock()),
        ):
            await StockForecastAlerts().tick()
        notification_service._template_cache.clear()

        assert sorted((name, event) for name, event, _ in heard) == [
            ("both", "stock_break_alert"),
            ("break-only", "stock_break_alert"),
            ("reorder-only", "stock_reorder_alert"),
        ]
        inbox = (
            await db_session.execute(
                select(User.username, UserNotification.event_type).join(User, User.id == UserNotification.user_id)
            )
        ).all()
        assert sorted(inbox) == [("u-both", "stock_break_alert"), ("u-reorder", "stock_reorder_alert")]

    @pytest.mark.asyncio
    async def test_a_telegram_chat_is_judged_by_its_own_subscription(self, db_session) -> None:
        """Telegram's event gate is the chat: the reorder copy carries a filter
        that passes only chats which did not ask for the break event."""
        service = NotificationService()
        sent = AsyncMock()
        with (
            patch.object(
                service, "_get_providers_for_event", AsyncMock(return_value=[SimpleNamespace(provider_type="telegram")])
            ),
            patch.object(service, "_build_message_from_template", AsyncMock(return_value=("t", "b"))),
            patch.object(service, "_send_to_providers", sent),
        ):
            await service._send_stock_reorder(
                "PLA", None, 1.0, 1.0, 1, db_session, subtype=None, color=None, skip_break_subscribers=True
            )

        chat_filter = sent.await_args.kwargs["chat_filter"]
        assert chat_filter(SimpleNamespace(notify_events=["stock_reorder_alert"])) is True
        assert chat_filter(SimpleNamespace(notify_events=["stock_reorder_alert", "stock_break_alert"])) is False

    @pytest.mark.asyncio
    async def test_a_plain_reorder_reaches_everyone_subscribed_to_it(self, db_session, monkeypatch) -> None:
        """The restriction belongs to the break's copy alone."""
        from backend.app.services.notification_service import notification_service

        await _seed_templates(db_session, "en")
        db_session.add_all(
            [
                self._provider("reorder-only", ["on_stock_reorder_alert"]),
                self._provider("both", ["on_stock_reorder_alert", "on_stock_break_alert"]),
                _setting("forecast_global_lead_time_days", "7"),
            ]
        )
        await db_session.commit()
        await _sku(db_session, color_name="Black", subtype="Basic", remaining=900.0)  # at the reorder point

        heard: list[tuple[str, str]] = []

        async def capture(provider, title, message, db=None, **kwargs):
            heard.append((provider.name, kwargs["event_type"]))
            return True, ""

        notification_service._template_cache.clear()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch.object(notification_service, "_send_to_provider", capture),
            patch.object(notification_service, "_update_provider_status", AsyncMock()),
            patch.object(notification_service, "_log_notification", AsyncMock()),
        ):
            await StockForecastAlerts().tick()
        notification_service._template_cache.clear()

        assert sorted(heard) == [("both", "stock_reorder_alert"), ("reorder-only", "stock_reorder_alert")]


class TestTheLegacyColourlessRowIsNotShadowed:
    @pytest.mark.asyncio
    async def test_a_coloured_sku_keeps_the_lead_time_it_was_forecast_with(self, db_session, monkeypatch) -> None:
        """Legacy row (PLA, Basic, Bambu, no colour): lead time 10 d, margin 500 g.
        Black: 240 g at 30 g/day is 8 days — inside 10, a break. Six hours later
        it must still be one, and its lead time still 10."""
        db_session.add(
            FilamentSkuSettings(
                material="PLA",
                subtype="Basic",
                brand="Bambu",
                color_name=None,
                lead_time_days=10,
                safety_margin_value=500,
                safety_margin_unit="g",
                alerts_snoozed=False,
            )
        )
        await db_session.commit()
        await _sku(db_session, color_name="Black", subtype="Basic", remaining=240.0, per_day=30.0)

        breakage, reorder = AsyncMock(), AsyncMock()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch("backend.app.services.notification_service.notification_service.on_stock_break_alert", breakage),
            patch("backend.app.services.notification_service.notification_service.on_stock_reorder_alert", reorder),
        ):
            await StockForecastAlerts().tick()
            await StockForecastAlerts().tick()

        alerts = await find_stock_alerts(db_session)
        assert [(a.kind, a.lead_time_days) for a in alerts] == [("break", 10)]
        assert breakage.await_count == 1  # announced once, not re-announced as something else
        assert reorder.await_count == 0

        rows = (await db_session.execute(select(FilamentSkuSettings))).scalars().all()
        own = next(row for row in rows if row.color_name == "Black")
        assert (own.lead_time_days, own.safety_margin_value, own.safety_margin_unit) == (10, 500, "g")
        assert own.stock_break_notified_at is not None
        legacy = next(row for row in rows if row.color_name is None)
        assert legacy.stock_break_notified_at is None  # other colours still fall back to a clean row


class TestAColourThatRanOutSaysSo:
    """Owner, 2026-11-20: a SKU whose last spool was archived is not "will run
    out before replenishment, 0 g left" — it ran out. The same subscription
    (the stock-break event) carries it; only the text differs."""

    @staticmethod
    def _depleted(service, db):
        return service.on_stock_depleted(
            material="PETG",
            subtype="Basic",
            color="Olive Green",
            brand="Sunlu",
            stock_g=0.0,
            rate_g_day=5.3,
            days_left=0,
            lead_time_days=3,
            db=db,
        )

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("lang", "title", "said", "not_said"),
        [
            ("en", "Out of stock: PETG Basic Olive Green", "ran out", "will run out"),
            ("uk", "Закінчився: PETG Basic Olive Green", "закінчився", "закінчиться"),
        ],
    )
    async def test_the_text_says_it_ran_out(self, db_session, lang, title, said, not_said) -> None:
        await _seed_templates(db_session, lang)

        sent_title, body = await _rendered(db_session, lambda s: self._depleted(s, db_session))

        assert sent_title == title
        assert "PETG Basic Olive Green (Sunlu) " + said in body
        assert not_said not in body and "3" in body and "5.3" in body

    @pytest.mark.asyncio
    async def test_each_recipient_hears_it_once_under_its_own_subscription(self, db_session, monkeypatch) -> None:
        from backend.app.services.notification_service import notification_service

        await _seed_templates(db_session, "en")
        db_session.add_all(
            [
                TestAReorderOnlyRecipientHearsAboutABreak._provider("reorder-only", ["on_stock_reorder_alert"]),
                TestAReorderOnlyRecipientHearsAboutABreak._provider(
                    "both", ["on_stock_reorder_alert", "on_stock_break_alert"]
                ),
                TestAReorderOnlyRecipientHearsAboutABreak._provider("break-only", ["on_stock_break_alert"]),
                User(username="u-reorder", password_hash="x", role="user", inbox_events=["stock_reorder_alert"]),
                User(username="u-break", password_hash="x", role="user", inbox_events=["stock_break_alert"]),
                _setting("forecast_global_lead_time_days", "7"),
            ]
        )
        await db_session.commit()
        spool = await _sku(db_session, color_name="Black", subtype="Basic", remaining=0.0)
        spool.archived_at = datetime.now(timezone.utc).replace(tzinfo=None)
        await db_session.commit()

        heard: list[tuple[str, str, str]] = []

        async def capture(provider, title, message, db=None, **kwargs):
            heard.append((provider.name, kwargs["event_type"], title))
            return True, ""

        notification_service._template_cache.clear()
        monkeypatch.setattr("backend.app.services.stock_forecast_alerts.async_session", _ctx(db_session))
        with (
            patch.object(notification_service, "_send_to_provider", capture),
            patch.object(notification_service, "_update_provider_status", AsyncMock()),
            patch.object(notification_service, "_log_notification", AsyncMock()),
        ):
            await StockForecastAlerts().tick()
        notification_service._template_cache.clear()

        title = "Out of stock: PLA Basic Black"
        assert sorted(heard) == [
            ("both", "stock_break_alert", title),
            ("break-only", "stock_break_alert", title),
            ("reorder-only", "stock_reorder_alert", title),
        ]
        inbox = (
            await db_session.execute(
                select(User.username, UserNotification.event_type, UserNotification.title).join(
                    User, User.id == UserNotification.user_id
                )
            )
        ).all()
        assert sorted(inbox) == [("u-break", "stock_break_alert", title), ("u-reorder", "stock_reorder_alert", title)]


def _setting(key: str, value: str):
    from backend.app.models.settings import Settings

    return Settings(key=key, value=value)


async def _sku(db, *, color_name: str, subtype: str, remaining: float, per_day: float = 100.0) -> Spool:
    """One 1000 g spool of (PLA, subtype, Bambu, colour) used ``per_day`` on each
    of the last five days, anchored to the clock the tick reads."""
    anchor = datetime.now(timezone.utc)
    spool = Spool(
        material="PLA",
        subtype=subtype,
        brand="Bambu",
        color_name=color_name,
        label_weight=1000,
        weight_used=1000.0 - remaining,
        weight_used_baseline=0.0,
        created_at=(anchor - timedelta(days=90)).replace(tzinfo=None),
    )
    db.add(spool)
    await db.commit()
    for days_ago in (5, 4, 3, 2, 1):
        db.add(
            SpoolUsageHistory(
                spool_id=spool.id,
                weight_used=per_day,
                created_at=(anchor - timedelta(days=days_ago)).replace(tzinfo=None),
            )
        )
    await db.commit()
    return spool


def _ctx(db_session):
    from contextlib import asynccontextmanager

    @asynccontextmanager
    async def _session_ctx():
        yield db_session

    return _session_ctx
