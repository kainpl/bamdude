"""Manager for smart plug automation and delayed turn-off."""

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.tasks import spawn_background_task
from backend.app.services.homeassistant import homeassistant_service
from backend.app.services.mqtt_smart_plug import mqtt_smart_plug_service
from backend.app.services.printer_manager import printer_manager
from backend.app.services.rest_smart_plug import rest_smart_plug_service
from backend.app.services.tasmota import tasmota_service

if TYPE_CHECKING:
    from backend.app.models.smart_plug import SmartPlug

logger = logging.getLogger(__name__)


class UnknownPlugTypeError(ValueError):
    """A plug type no driver claims.

    Raised rather than defaulted. A default here is not a fallback, it is a wrong
    answer delivered confidently: the old ``return tasmota_service`` sent an
    HTTP poll to an ``ip_address`` the plug does not have, and fed the result
    into per-print energy.
    """

    def __init__(self, plug_type: str | None):
        super().__init__(f"No smart-plug driver for plug_type {plug_type!r} — add it to get_service_for_plug()")
        self.plug_type = plug_type


class SmartPlugManager:
    """Manages smart plug automation and delayed turn-off."""

    def __init__(self):
        self._pending_off: dict[int, asyncio.Task] = {}  # plug_id -> task
        self._loop: asyncio.AbstractEventLoop | None = None
        self._scheduler_task: asyncio.Task | None = None
        self._snapshot_task: asyncio.Task | None = None
        self._last_schedule_check: dict[int, str] = {}  # plug_id -> "HH:MM" last executed

    async def get_service_for_plug(self, plug: "SmartPlug", db: AsyncSession | None = None):
        """The single place a plug type becomes a driver.

        For HA plugs, configures the service with current settings from DB.

        Every type is named explicitly and an unknown one raises. It used to end
        in a bare ``return tasmota_service``, which meant a type this chain had
        not been taught about did not fail — it quietly got Tasmota's answer, an
        HTTP poll against an ``ip_address`` that such a plug does not have. Since
        this feeds per-print energy, that is a WRONG number rather than a missing
        one, and nothing anywhere looks broken.

        It was harmless only by luck: every shipping type happened to be listed.
        The mqtt branch had to be added by m113 for exactly this reason, and the
        zigbee one would have been the next instance. A total mapping means the
        next type added is a loud error at the one site that has to know, instead
        of silent Tasmota everywhere downstream.
        """
        if plug.plug_type == "tasmota":
            return tasmota_service
        if plug.plug_type == "homeassistant":
            # Configure HA service with current settings
            await self._configure_ha_service(db)
            return homeassistant_service
        if plug.plug_type == "rest":
            return rest_smart_plug_service
        if plug.plug_type == "mqtt":
            return mqtt_smart_plug_service
        if plug.plug_type == "zigbee":
            # Imported here rather than at module scope: the zigbee package
            # pulls zigpy in, and this module is imported during startup long
            # before the coordinator exists.
            from backend.app.services.zigbee.driver import zigbee_smart_plug_service

            return zigbee_smart_plug_service
        raise UnknownPlugTypeError(plug.plug_type)

    async def _configure_ha_service(self, db: AsyncSession | None = None):
        """Configure the HA service with URL and token from settings."""
        from backend.app.api.routes.settings import get_homeassistant_settings

        try:
            if db:
                # Use provided session
                ha_settings = await get_homeassistant_settings(db)
            else:
                # Create new session
                from backend.app.core.database import async_session

                async with async_session() as session:
                    ha_settings = await get_homeassistant_settings(session)

            homeassistant_service.configure(ha_settings["ha_url"], ha_settings["ha_token"])
        except Exception as e:
            logger.warning("Failed to configure HA service: %s", e)

    def set_event_loop(self, loop: asyncio.AbstractEventLoop):
        """Set the event loop for async operations."""
        self._loop = loop

    def start_scheduler(self):
        """Start the background scheduler for time-based plug control."""
        if self._scheduler_task is None:
            self._scheduler_task = asyncio.create_task(self._schedule_loop())
            logger.info("Smart plug scheduler started")
        if self._snapshot_task is None:
            self._snapshot_task = asyncio.create_task(self._snapshot_loop())
            logger.info("Smart plug energy snapshot loop started")

    def stop_scheduler(self):
        """Stop the background scheduler."""
        if self._scheduler_task:
            self._scheduler_task.cancel()
            self._scheduler_task = None
            logger.info("Smart plug scheduler stopped")
        if self._snapshot_task:
            self._snapshot_task.cancel()
            self._snapshot_task = None
            logger.info("Smart plug energy snapshot loop stopped")

    async def _schedule_loop(self):
        """Background loop that checks scheduled on/off times every minute."""
        while True:
            try:
                await self._check_schedules()
            except Exception as e:
                logger.error("Error in schedule check: %s", e)

            # Wait until the next minute
            await asyncio.sleep(60)

    async def _snapshot_loop(self):
        """Background loop that captures each plug's lifetime energy counter hourly.

        Powers date-range queries in "total consumption" energy mode (upstream #941).
        Takes a snapshot shortly after startup so the first bucket isn't empty,
        then every hour.
        """
        # Short warm-up delay so other services finish booting; still gives us
        # an initial snapshot well before the first hour mark.
        await asyncio.sleep(30)
        while True:
            try:
                await self._capture_energy_snapshots()
            except Exception as e:
                logger.error("Error in energy snapshot capture: %s", e)
            await asyncio.sleep(3600)  # 1 hour

    async def _capture_energy_snapshots(self):
        """Capture one energy snapshot row per plug with a usable lifetime counter."""
        from backend.app.core.database import async_session
        from backend.app.models.smart_plug import SmartPlug
        from backend.app.models.smart_plug_energy_snapshot import SmartPlugEnergySnapshot

        async with async_session() as db:
            plugs_result = await db.execute(select(SmartPlug).where(SmartPlug.enabled.is_(True)))
            plugs = list(plugs_result.scalars().all())
            if not plugs:
                return

            now = datetime.now(timezone.utc)
            captured = 0
            for plug in plugs:
                try:
                    service = await self.get_service_for_plug(plug, db)
                    energy = await service.get_energy(plug)
                except Exception as e:
                    logger.debug("Snapshot: failed to read energy from plug %s: %s", plug.id, e)
                    continue
                if not energy:
                    continue
                lifetime = energy.get("total")
                if lifetime is None:
                    # No lifetime counter configured for this plug — a figure
                    # that resets cannot feed cumulative snapshots.
                    continue
                db.add(
                    SmartPlugEnergySnapshot(
                        plug_id=plug.id,
                        recorded_at=now,
                        lifetime_kwh=float(lifetime),
                    )
                )
                captured += 1

            if captured:
                await db.commit()
                logger.info("Captured %d energy snapshot(s)", captured)

    async def _check_schedules(self):
        """Check all plugs for scheduled on/off times."""
        from backend.app.core.database import async_session
        from backend.app.models.smart_plug import SmartPlug

        current_time = datetime.now().strftime("%H:%M")

        async with async_session() as db:
            result = await db.execute(
                select(SmartPlug).where(
                    SmartPlug.enabled.is_(True),
                    SmartPlug.schedule_enabled.is_(True),
                )
            )
            plugs = result.scalars().all()

            for plug in plugs:
                # Per-plug, deliberately: this loop drives scheduled power for
                # the whole farm, and it had no guard at all. One plug that
                # cannot be resolved or reached would abort the pass, so every
                # plug after it silently missed its schedule — a printer left on
                # overnight because an unrelated plug was misconfigured. The
                # monitor loop above already isolates per plug; this one now
                # does too.
                try:
                    service = await self.get_service_for_plug(plug, db)
                except Exception:
                    logger.exception("Schedule: no driver for plug '%s', skipping it this pass", plug.name)
                    continue

                # Check if we should turn on
                if plug.schedule_on_time == current_time:
                    last_check = self._last_schedule_check.get(plug.id)
                    if last_check != f"on:{current_time}":
                        logger.info("Schedule: Turning on plug '%s' at %s", plug.name, current_time)
                        success = await service.turn_on(plug)
                        if success:
                            plug.last_state = "ON"
                            plug.last_checked = datetime.now(timezone.utc)
                            self._last_schedule_check[plug.id] = f"on:{current_time}"

                # Check if we should turn off
                if plug.schedule_off_time == current_time:
                    last_check = self._last_schedule_check.get(plug.id)
                    if last_check != f"off:{current_time}":
                        logger.info("Schedule: Turning off plug '%s' at %s", plug.name, current_time)
                        success = await service.turn_off(plug)
                        if success:
                            plug.last_state = "OFF"
                            plug.last_checked = datetime.now(timezone.utc)
                            self._last_schedule_check[plug.id] = f"off:{current_time}"
                            # Mark printer offline if linked — but only for the plug
                            # that feeds its mains, not an accessory (#2629).
                            if plug.printer_id and plug.controls_printer_power:
                                printer_manager.mark_printer_offline(plug.printer_id)

            await db.commit()

    async def _get_plugs_for_printer(self, printer_id: int, db: AsyncSession) -> "list[SmartPlug]":
        """Get all smart plugs linked to a printer (#903).

        Returns all plugs so that on_print_start/on_print_complete can iterate
        over them (e.g., a power plug + an air filter switch).
        """
        from backend.app.models.smart_plug import SmartPlug

        result = await db.execute(select(SmartPlug).where(SmartPlug.printer_id == printer_id))
        return list(result.scalars().all())

    async def on_print_start(self, printer_id: int, db: AsyncSession):
        """Called when a print starts - turn on plug if configured."""
        plugs = await self._get_plugs_for_printer(printer_id, db)

        for plug in plugs:
            if not plug.enabled:
                logger.debug("Smart plug '%s' is disabled, skipping auto-on", plug.name)
                continue

            # Cancel any pending off task FIRST — a re-print must abort a
            # scheduled auto-off regardless of the plug's auto_on setting
            # (#1890). Previously this lived behind the auto_on gate, so a plug
            # with auto_on disabled kept its pending off and cut power mid-print.
            self._cancel_pending_off(plug.id)

            if not plug.auto_on:
                logger.debug("Smart plug '%s' auto_on is disabled", plug.name)
                continue

            # Turn on the plug
            logger.info("Print started on printer %s, turning on plug '%s'", printer_id, plug.name)
            service = await self.get_service_for_plug(plug, db)
            success = await service.turn_on(plug)

            if success:
                # Update last state and reset auto_off_executed
                plug.last_state = "ON"
                plug.last_checked = datetime.now(timezone.utc)
                plug.auto_off_executed = False  # Reset flag when turning on
                await db.commit()

    async def on_print_complete(self, printer_id: int, status: str, db: AsyncSession):
        """Called when a print completes - schedule turn off if configured.

        Only triggers auto-off on successful completion (status='completed').
        Failed prints keep the printer powered on for user investigation.
        """
        plugs = await self._get_plugs_for_printer(printer_id, db)

        for plug in plugs:
            if not plug.enabled:
                logger.debug("Smart plug '%s' is disabled, skipping auto-off", plug.name)
                continue

            if not plug.auto_off:
                logger.debug("Smart plug '%s' auto_off is disabled", plug.name)
                continue

            # Skip auto-off for HA script entities (scripts can only be triggered, not turned off)
            if plug.plug_type == "homeassistant" and plug.ha_entity_id and plug.ha_entity_id.startswith("script."):
                logger.debug("Smart plug '%s' is a HA script entity, skipping auto-off", plug.name)
                continue

            # Only auto-off on successful completion, not on failures
            # This allows the user to investigate errors before power-off
            if status != "completed":
                logger.info(
                    f"Print on printer {printer_id} ended with status '{status}', "
                    f"skipping auto-off for plug '{plug.name}' to allow investigation"
                )
                continue

            logger.info(
                "Print completed successfully on printer %s, scheduling turn-off for plug '%s'",
                printer_id,
                plug.name,
            )

            self._schedule_off_per_mode(plug, printer_id)

    def _schedule_off_per_mode(self, plug: "SmartPlug", printer_id: int):
        """Schedule an auto-off using the plug's configured off strategy.

        Honours the per-plug ``off_delay_mode`` — ``time`` waits
        ``off_delay_minutes``; ``temperature`` waits until the nozzle drops
        below ``off_temp_threshold`` (#1890 — the queue/scheduler auto-off
        paths used to hardcode 50°C / 600s and ignore these settings). Both
        branches register a cancellable task in ``_pending_off``, so a re-print
        cancels the pending off via :meth:`on_print_start`.
        """
        if plug.off_delay_mode == "temperature":
            self._schedule_temp_based_off(plug, printer_id, plug.off_temp_threshold)
        else:
            # Default / "time": also the safe fallback for any unexpected value.
            self._schedule_delayed_off(plug, printer_id, plug.off_delay_minutes * 60)

    async def schedule_off_after_queue_job(self, printer_id: int, db: AsyncSession):
        """Schedule auto-off for a printer after a queue job that opted in.

        The print-queue "auto off after this job" toggle (``auto_off_after``) is
        a per-job override, independent of the plug's global ``auto_off`` flag —
        so unlike :meth:`on_print_complete` this does NOT gate on ``plug.auto_off``.
        It still honours ``enabled`` and skips HA-script entities (which can only
        be triggered, not turned off), and uses each plug's configured off
        strategy via :meth:`_schedule_off_per_mode`. Replaces the three inline
        ``wait_for_cooldown(50°C, 600s)`` blocks that ignored plug settings,
        fired on the cooldown *timeout* regardless of print state, and could not
        be cancelled by a re-print (#1890).
        """
        plugs = await self._get_plugs_for_printer(printer_id, db)
        for plug in plugs:
            if not plug.enabled:
                logger.debug("Smart plug '%s' is disabled, skipping queue auto-off", plug.name)
                continue
            if plug.plug_type == "homeassistant" and plug.ha_entity_id and plug.ha_entity_id.startswith("script."):
                logger.debug("Smart plug '%s' is a HA script entity, skipping queue auto-off", plug.name)
                continue
            logger.info(
                "Queue job finished on printer %s, scheduling turn-off for plug '%s'",
                printer_id,
                plug.name,
            )
            self._schedule_off_per_mode(plug, printer_id)

    async def on_drying_complete(self, printer_id: int, db: AsyncSession):
        """Schedule turn-off for plugs flagged ``auto_off_after_drying`` when
        an AMS drying cycle finishes on this printer (upstream Bambuddy
        #1349).

        Mirrors :meth:`on_print_complete` but uses the drying-specific
        toggle and delay. Iterates every plug linked to the printer and
        fires only on the ones the user has opted-in via the per-plug
        toggle. Always uses the time-delay branch — temperature-based
        cooldown is about the printer's hotend, which isn't meaningful
        after a drying cycle (the AMS chamber is the thing that's hot,
        and BamDude doesn't track its temperature).
        """
        plugs = await self._get_plugs_for_printer(printer_id, db)
        if not plugs:
            return

        for plug in plugs:
            if not plug.enabled:
                logger.debug("Smart plug '%s' is disabled, skipping drying auto-off", plug.name)
                continue

            if not plug.auto_off_after_drying:
                logger.debug("Smart plug '%s' auto_off_after_drying is disabled, skipping", plug.name)
                continue

            # HA script entities can only be triggered, not turned off —
            # same guard the print-finish path uses.
            if plug.plug_type == "homeassistant" and plug.ha_entity_id and plug.ha_entity_id.startswith("script."):
                logger.debug("Smart plug '%s' is a HA script entity, skipping drying auto-off", plug.name)
                continue

            logger.info(
                "Drying completed on printer %s, scheduling turn-off for plug '%s' in %d min",
                printer_id,
                plug.name,
                plug.off_delay_after_drying_minutes,
            )
            self._schedule_delayed_off(plug, printer_id, plug.off_delay_after_drying_minutes * 60)

    def _schedule_delayed_off(self, plug: "SmartPlug", printer_id: int, delay_seconds: int):
        """Schedule turn-off after delay."""
        # Cancel any existing task for this plug
        self._cancel_pending_off(plug.id)

        logger.info("Scheduling turn-off for plug '%s' in %s seconds", plug.name, delay_seconds)

        # Mark as pending in database (survives restarts)
        spawn_background_task(self._mark_auto_off_pending(plug.id, True), name=f"mark-auto-off-pending-{plug.id}")

        task = asyncio.create_task(
            self._delayed_off(
                plug.id,
                plug.plug_type,
                plug.ip_address,
                plug.ha_entity_id,
                plug.username,
                plug.password,
                printer_id,
                delay_seconds,
                controls_printer_power=plug.controls_printer_power,
                rest_off_url=plug.rest_off_url if plug.plug_type == "rest" else None,
                rest_off_body=plug.rest_off_body if plug.plug_type == "rest" else None,
                rest_method=plug.rest_method if plug.plug_type == "rest" else None,
                rest_headers=plug.rest_headers if plug.plug_type == "rest" else None,
            )
        )
        self._pending_off[plug.id] = task

    async def _delayed_off(
        self,
        plug_id: int,
        plug_type: str,
        ip_address: str | None,
        ha_entity_id: str | None,
        username: str | None,
        password: str | None,
        printer_id: int,
        delay_seconds: int,
        *,
        # False for an accessory plug — don't blank the printer's state (#2629).
        # Defaults True so a caller that predates the flag keeps today's behaviour.
        controls_printer_power: bool = True,
        rest_off_url: str | None = None,
        rest_off_body: str | None = None,
        rest_method: str | None = None,
        rest_headers: str | None = None,
    ):
        """Wait and turn off."""
        try:
            await asyncio.sleep(delay_seconds)

            # #1890: never cut power while a print is loaded / running. The
            # delay fires unconditionally after N minutes, so if the user
            # re-started (or reprinted) in the meantime, the printer is active
            # again — skip the off and clear the pending flag rather than
            # killing the print mid-way.
            if printer_manager.is_print_active(printer_id):
                logger.info(
                    "Skipping auto-off for plug %s: printer %s is printing again (state=%s)",
                    plug_id,
                    printer_id,
                    getattr(printer_manager.get_status(printer_id), "state", "unknown"),
                )
                await self._mark_auto_off_pending(plug_id, False)
                return

            # Create a minimal plug-like object for the service
            class PlugInfo:
                def __init__(self):
                    self.plug_type = plug_type
                    self.ip_address = ip_address
                    self.ha_entity_id = ha_entity_id
                    self.username = username
                    self.password = password
                    self.name = f"plug_{plug_id}"
                    # REST fields
                    self.rest_off_url = rest_off_url
                    self.rest_off_body = rest_off_body
                    self.rest_method = rest_method
                    self.rest_headers = rest_headers

            plug_info = PlugInfo()
            service = await self.get_service_for_plug(plug_info)
            success = await service.turn_off(plug_info)
            logger.info("Turned off plug %s after time delay", plug_id)

            # Mark auto_off_executed in database and update printer status
            if success:
                await self._mark_auto_off_executed(plug_id)
                # Mark the printer as offline immediately — mains plug only (#2629)
                if controls_printer_power:
                    printer_manager.mark_printer_offline(printer_id)

        except asyncio.CancelledError:
            logger.debug("Delayed turn-off cancelled for plug %s", plug_id)
        finally:
            self._pending_off.pop(plug_id, None)

    def _schedule_temp_based_off(self, plug: "SmartPlug", printer_id: int, temp_threshold: int):
        """Monitor temperature and turn off when below threshold."""
        # Cancel any existing task for this plug
        self._cancel_pending_off(plug.id)

        logger.info("Scheduling temperature-based turn-off for plug '%s' (threshold: %s°C)", plug.name, temp_threshold)

        # Mark as pending in database (survives restarts)
        spawn_background_task(self._mark_auto_off_pending(plug.id, True), name=f"mark-auto-off-pending-{plug.id}")

        task = asyncio.create_task(
            self._temp_based_off(
                plug.id,
                plug.plug_type,
                plug.ip_address,
                plug.ha_entity_id,
                plug.username,
                plug.password,
                printer_id,
                temp_threshold,
                controls_printer_power=plug.controls_printer_power,
                rest_off_url=plug.rest_off_url if plug.plug_type == "rest" else None,
                rest_off_body=plug.rest_off_body if plug.plug_type == "rest" else None,
                rest_method=plug.rest_method if plug.plug_type == "rest" else None,
                rest_headers=plug.rest_headers if plug.plug_type == "rest" else None,
            )
        )
        self._pending_off[plug.id] = task

    async def _temp_based_off(
        self,
        plug_id: int,
        plug_type: str,
        ip_address: str | None,
        ha_entity_id: str | None,
        username: str | None,
        password: str | None,
        printer_id: int,
        temp_threshold: int,
        *,
        # False for an accessory plug — don't blank the printer's state (#2629).
        controls_printer_power: bool = True,
        rest_off_url: str | None = None,
        rest_off_body: str | None = None,
        rest_method: str | None = None,
        rest_headers: str | None = None,
    ):
        """Poll temperature until below threshold, then turn off.

        For dual-extruder printers (H2 series), checks both nozzles.
        """
        try:
            check_interval = 10  # seconds
            max_wait = 3600  # 1 hour max
            elapsed = 0

            while elapsed < max_wait:
                status = printer_manager.get_status(printer_id)

                if status:
                    temps = status.temperatures or {}
                    nozzle_temp = temps.get("nozzle", 999)
                    # Check second nozzle for dual-extruder printers (H2 series)
                    nozzle_2_temp = temps.get("nozzle_2")

                    # Get the maximum temperature across all nozzles
                    max_nozzle_temp = nozzle_temp
                    if nozzle_2_temp is not None:
                        max_nozzle_temp = max(nozzle_temp, nozzle_2_temp)
                        logger.info(
                            f"Temp check plug {plug_id}: nozzle1={nozzle_temp}°C, "
                            f"nozzle2={nozzle_2_temp}°C, max={max_nozzle_temp}°C, "
                            f"threshold={temp_threshold}°C"
                        )
                    else:
                        logger.info(
                            "Temp check plug %s: nozzle=%s°C, threshold=%s°C", plug_id, nozzle_temp, temp_threshold
                        )

                    if max_nozzle_temp < temp_threshold:
                        # #1890: the nozzle can dip below the threshold between
                        # a finished print and a fresh one starting (e.g. a
                        # touchscreen reprint during the PREPARE/heating phase).
                        # Guard the turn-off so we never cut power on a loaded
                        # print; keep polling until it's genuinely idle again.
                        if printer_manager.is_print_active(printer_id):
                            logger.info(
                                "Deferring temp-based auto-off for plug %s: printer %s is printing again (state=%s)",
                                plug_id,
                                printer_id,
                                getattr(printer_manager.get_status(printer_id), "state", "unknown"),
                            )
                            await asyncio.sleep(check_interval)
                            elapsed += check_interval
                            continue

                        # All nozzles are below threshold, turn off
                        class PlugInfo:
                            def __init__(self):
                                self.plug_type = plug_type
                                self.ip_address = ip_address
                                self.ha_entity_id = ha_entity_id
                                self.username = username
                                self.password = password
                                self.name = f"plug_{plug_id}"
                                # REST fields
                                self.rest_off_url = rest_off_url
                                self.rest_off_body = rest_off_body
                                self.rest_method = rest_method
                                self.rest_headers = rest_headers

                        plug_info = PlugInfo()
                        service = await self.get_service_for_plug(plug_info)
                        success = await service.turn_off(plug_info)
                        logger.info(
                            f"Turned off plug {plug_id} after nozzle temp dropped to "
                            f"{max_nozzle_temp}°C (threshold: {temp_threshold}°C)"
                        )

                        # Mark auto_off_executed in database and update printer status
                        if success:
                            await self._mark_auto_off_executed(plug_id)
                            # Mark the printer as offline immediately — mains plug only (#2629)
                            if controls_printer_power:
                                printer_manager.mark_printer_offline(printer_id)

                        break

                await asyncio.sleep(check_interval)
                elapsed += check_interval

            if elapsed >= max_wait:
                logger.warning("Temperature-based turn-off timed out for plug %s after %ss", plug_id, max_wait)

        except asyncio.CancelledError:
            logger.debug("Temperature-based turn-off cancelled for plug %s", plug_id)
        finally:
            self._pending_off.pop(plug_id, None)

    async def _mark_auto_off_pending(self, plug_id: int, pending: bool):
        """Mark a plug as having a pending auto-off (survives restarts)."""
        try:
            from backend.app.core.database import async_session
            from backend.app.models.smart_plug import SmartPlug

            async with async_session() as db:
                result = await db.execute(select(SmartPlug).where(SmartPlug.id == plug_id))
                plug = result.scalar_one_or_none()
                if plug:
                    plug.auto_off_pending = pending
                    plug.auto_off_pending_since = datetime.now(timezone.utc) if pending else None
                    await db.commit()
                    logger.debug("Marked plug %s auto_off_pending=%s", plug_id, pending)
        except Exception as e:
            logger.warning("Failed to update plug %s pending state: %s", plug_id, e)

    async def _mark_auto_off_executed(self, plug_id: int):
        """Disable auto-off after it was executed (one-shot behavior unless persistent)."""
        try:
            from backend.app.core.database import async_session
            from backend.app.models.smart_plug import SmartPlug

            async with async_session() as db:
                result = await db.execute(select(SmartPlug).where(SmartPlug.id == plug_id))
                plug = result.scalar_one_or_none()
                if plug:
                    if not plug.auto_off_persistent:
                        plug.auto_off = False  # Disable auto-off (one-shot behavior)
                    plug.auto_off_executed = False  # Reset the flag
                    plug.auto_off_pending = False  # Clear pending state
                    plug.auto_off_pending_since = None
                    plug.last_state = "OFF"
                    plug.last_checked = datetime.now(timezone.utc)
                    await db.commit()
                    if plug.auto_off_persistent:
                        logger.info("Auto-off executed for plug %s (persistent, stays enabled)", plug_id)
                    else:
                        logger.info("Auto-off executed and disabled for plug %s", plug_id)
        except Exception as e:
            logger.warning("Failed to update plug %s after auto-off: %s", plug_id, e)

    def _cancel_pending_off(self, plug_id: int):
        """Cancel any pending off task for this plug."""
        if plug_id in self._pending_off:
            logger.debug("Cancelling pending turn-off for plug %s", plug_id)
            self._pending_off[plug_id].cancel()
            del self._pending_off[plug_id]
            # Clear pending state in database
            spawn_background_task(self._mark_auto_off_pending(plug_id, False), name=f"mark-auto-off-pending-{plug_id}")

    def cancel_all_pending(self):
        """Cancel all pending turn-off tasks."""
        for plug_id in list(self._pending_off.keys()):
            self._cancel_pending_off(plug_id)

    async def resume_pending_auto_offs(self):
        """Resume any pending auto-offs that were interrupted by a restart.

        Called on startup to check for plugs that had auto-off pending but
        never completed (e.g., due to service restart).
        """
        try:
            from backend.app.core.database import async_session
            from backend.app.models.smart_plug import SmartPlug

            async with async_session() as db:
                # Find all plugs with pending auto-off
                result = await db.execute(
                    select(SmartPlug).where(
                        SmartPlug.auto_off_pending.is_(True),
                        SmartPlug.printer_id.isnot(None),
                    )
                )
                pending_plugs = result.scalars().all()

                for plug in pending_plugs:
                    # Check how long it's been pending (timeout after 2 hours)
                    if plug.auto_off_pending_since:
                        pending_since = plug.auto_off_pending_since
                        if pending_since.tzinfo is None:
                            pending_since = pending_since.replace(tzinfo=timezone.utc)
                        elapsed = (datetime.now(timezone.utc) - pending_since).total_seconds()
                        if elapsed > 7200:  # 2 hours
                            logger.warning(
                                f"Auto-off for plug '{plug.name}' was pending for {elapsed / 60:.0f} minutes, "
                                f"clearing stale pending state"
                            )
                            plug.auto_off_pending = False
                            plug.auto_off_pending_since = None
                            await db.commit()
                            continue

                    logger.info("Resuming pending auto-off for plug '%s' (printer %s)", plug.name, plug.printer_id)

                    # #1890: never resume a power-off onto a live print. If the
                    # printer started a new print during the downtime, the stale
                    # pending off must be dropped, not executed — same guard the
                    # live off-executors use.
                    if printer_manager.is_print_active(plug.printer_id):
                        logger.info(
                            "Not resuming auto-off for plug '%s': printer %s is printing (state=%s); clearing pending",
                            plug.name,
                            plug.printer_id,
                            getattr(printer_manager.get_status(plug.printer_id), "state", "unknown"),
                        )
                        plug.auto_off_pending = False
                        plug.auto_off_pending_since = None
                        await db.commit()
                        continue

                    # Resume the appropriate off mode
                    if plug.off_delay_mode == "temperature":
                        self._schedule_temp_based_off(plug, plug.printer_id, plug.off_temp_threshold)
                    else:
                        # For time mode, just turn off immediately since delay already passed
                        logger.info("Time-based auto-off was pending, turning off plug '%s' now", plug.name)

                        service = await self.get_service_for_plug(plug, db)
                        success = await service.turn_off(plug)
                        if success:
                            await self._mark_auto_off_executed(plug.id)
                            if plug.controls_printer_power:
                                printer_manager.mark_printer_offline(plug.printer_id)

                if pending_plugs:
                    logger.info("Resumed %s pending auto-off(s)", len(pending_plugs))

        except Exception as e:
            logger.warning("Failed to resume pending auto-offs: %s", e)


# Global singleton
smart_plug_manager = SmartPlugManager()
