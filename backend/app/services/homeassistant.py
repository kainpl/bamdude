"""Service for communicating with Home Assistant via REST API."""

import asyncio
import logging
import math
import ssl
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

if TYPE_CHECKING:
    from backend.app.models.smart_plug import SmartPlug

logger = logging.getLogger(__name__)

# Why a connection test failed: a closed set of codes, each with its fixed English sentence. The
# settings page shows the reason by its CODE, in the interface language; the sentence is for an API
# caller. Nothing here ever repeats the address, the token or an exception's own text.
HA_TEST_FAILURES = {
    "invalid_url": "The Home Assistant address is not accepted. Use an http or https address of the server.",
    "unauthorized": "Home Assistant refused the access token.",
    "unreachable": "Nothing answered at this address. Check the host, the port and the network.",
    "timeout": "Home Assistant did not answer in time.",
    "certificate_untrusted": "Home Assistant's certificate is not issued by an authority this server trusts.",
    "certificate_mismatch": "Home Assistant's certificate was issued for another host name.",
    "certificate_expired": "Home Assistant's certificate has expired.",
    "not_https": "The secure connection failed. This address and port may be serving plain http.",
    "redirect": "The server answered with a redirect. Enter the address Home Assistant itself answers at.",
    "unexpected_answer": "The server answered, but not as Home Assistant's API does.",
}


def tls_failure(exc: BaseException) -> str | None:
    """The reason code of a TLS failure anywhere in an exception's chain, or None when it is not TLS.

    httpx raises an untrusted certificate as ``ConnectError`` — the same as an address nothing
    answers on — so the cause chain is what tells them apart (upstream Bambuddy #3300).
    """
    seen: set[int] = set()
    cause: BaseException | None = exc
    while cause is not None and id(cause) not in seen:
        seen.add(id(cause))
        if isinstance(cause, ssl.SSLCertVerificationError):
            said = str(cause.verify_message or cause.reason or "").lower()
            if "mismatch" in said:
                return "certificate_mismatch"
            if "expired" in said:
                return "certificate_expired"
            return "certificate_untrusted"
        if isinstance(cause, ssl.SSLError):
            return "not_https"
        cause = cause.__cause__ or cause.__context__
    return None


def _log_destination(url: str) -> str:
    """Scheme, host and port of an address for a log line: no user info, path or query."""
    try:
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        return f"{parsed.scheme}://{host}" + (f":{parsed.port}" if parsed.port else "")
    except ValueError:
        return "?"


class HomeAssistantService:
    """Service for controlling Home Assistant entities via REST API."""

    def __init__(self, timeout: float = 10.0):
        self.timeout = timeout
        self.base_url: str = ""
        self.token: str = ""
        self._config_generation = 0

    def configure(self, url: str, token: str):
        """Configure HA connection settings.

        The URL is checked here — the one point it enters this service — under
        the LAN-service policy: Home Assistant on the same box or LAN is the
        normal deployment, so loopback and RFC-1918 stay allowed, while cloud
        metadata endpoints, numeric-encoded IPs and non-HTTP schemes do not.

        ``schemas/settings`` validates the same value on save; this covers the
        ones that were stored before that validator existed, and any that arrive
        by another route. An unsafe URL leaves the service unconfigured rather
        than raising, so a bad setting disables the integration instead of
        breaking whatever was calling it.
        """
        from backend.app.api.routes._url_safety import assert_safe_lan_service_url

        if url:
            try:
                assert_safe_lan_service_url(url, label="Home Assistant URL")
            except ValueError as exc:
                logger.warning("Refusing unsafe Home Assistant URL: %s", exc)
                url = ""

        base_url = url.rstrip("/") if url else ""
        token = token or ""
        if (base_url, token) != (self.base_url, self.token):
            self._config_generation += 1
        self.base_url = base_url
        self.token = token

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
        }

    async def get_status(self, plug: "SmartPlug") -> dict:
        """Get current state of HA entity.

        Returns dict with:
            - state: "ON" or "OFF" or None if unreachable
            - reachable: bool
            - device_name: str or None
        """
        if not self.base_url or not self.token:
            return {"state": None, "reachable": False, "device_name": None}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{self.base_url}/api/states/{plug.ha_entity_id}",
                    headers=self._headers(),
                )
                response.raise_for_status()
                data = response.json()

                state_value = data.get("state", "").lower()
                # Normalize to ON/OFF
                if state_value == "on":
                    state = "ON"
                elif state_value == "off":
                    state = "OFF"
                else:
                    state = None

                return {
                    "state": state,
                    "reachable": True,
                    "device_name": data.get("attributes", {}).get("friendly_name"),
                }
        except Exception as e:
            logger.warning("Failed to get HA entity state for %s: %s", plug.ha_entity_id, e)
            return {"state": None, "reachable": False, "device_name": None}

    async def turn_on(self, plug: "SmartPlug") -> bool:
        """Turn on HA entity. Returns True if successful."""
        success = await self._call_service(plug, "turn_on")
        if success:
            logger.info("Turned ON HA entity '%s' (%s)", plug.name, plug.ha_entity_id)
        return success

    async def turn_off(self, plug: "SmartPlug") -> bool:
        """Turn off HA entity. Returns True if successful."""
        success = await self._call_service(plug, "turn_off")
        if success:
            logger.info("Turned OFF HA entity '%s' (%s)", plug.name, plug.ha_entity_id)
        return success

    async def toggle(self, plug: "SmartPlug") -> bool:
        """Toggle HA entity. Returns True if successful."""
        success = await self._call_service(plug, "toggle")
        if success:
            logger.info("Toggled HA entity '%s' (%s)", plug.name, plug.ha_entity_id)
        return success

    async def _call_service(self, plug: "SmartPlug", action: str) -> bool:
        """Call HA service on entity."""
        if not self.base_url or not self.token or not plug.ha_entity_id:
            return False

        domain = plug.ha_entity_id.split(".")[0]  # "switch", "light", etc.

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/services/{domain}/{action}",
                    headers=self._headers(),
                    json={"entity_id": plug.ha_entity_id},
                )
                response.raise_for_status()
                return True
        except Exception as e:
            logger.warning("Failed to %s HA entity %s: %s", action, plug.ha_entity_id, e)
            return False

    async def get_energy(self, plug: "SmartPlug") -> dict | None:
        """Get energy data from HA sensor entities or switch attributes.

        First tries dedicated sensor entities if configured, then falls back
        to checking the switch entity's attributes.
        Returns dict with energy data or None if not available.
        """
        if not self.base_url or not self.token:
            return None

        power = None
        today = None
        total = None

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                # Fetch power from dedicated sensor entity if configured
                if plug.ha_power_entity:
                    power = await self._get_sensor_value(client, plug.ha_power_entity)

                # Fetch today's energy from dedicated sensor entity if configured
                if plug.ha_energy_today_entity:
                    today = await self._get_sensor_value(client, plug.ha_energy_today_entity)

                # Fetch total energy from dedicated sensor entity if configured
                if plug.ha_energy_total_entity:
                    total = await self._get_sensor_value(client, plug.ha_energy_total_entity)

                # Fallback: try switch entity attributes (original behavior)
                if power is None:
                    response = await client.get(
                        f"{self.base_url}/api/states/{plug.ha_entity_id}",
                        headers=self._headers(),
                    )
                    response.raise_for_status()
                    attrs = response.json().get("attributes", {})
                    power = attrs.get("current_power_w") or attrs.get("power")
                    if today is None:
                        today = attrs.get("today_energy_kwh")
                    if total is None:
                        total = attrs.get("total_energy_kwh")

                if power is None:
                    return None

                return {
                    "power": power,
                    "voltage": None,
                    "current": None,
                    "today": today,
                    "total": total,
                    "yesterday": None,
                    "factor": None,
                    "apparent_power": None,
                    "reactive_power": None,
                }
        except Exception as e:
            logger.debug("Failed to get HA energy data: %s", e)
            return None

    async def _get_sensor_value(self, client: httpx.AsyncClient, entity_id: str) -> float | None:
        """Fetch numeric value from a HA sensor entity."""
        try:
            response = await client.get(
                f"{self.base_url}/api/states/{entity_id}",
                headers=self._headers(),
            )
            response.raise_for_status()
            state = response.json().get("state")
            if state and state not in ("unknown", "unavailable"):
                return float(state)
        except Exception:
            pass  # Sensor read is best-effort; caller handles None
        return None

    @staticmethod
    def _validate_url(url: str) -> str | None:
        """Apply the same LAN URL policy as saved HA settings."""
        from backend.app.api.routes._url_safety import assert_safe_lan_service_url

        try:
            assert_safe_lan_service_url(url, label="Home Assistant URL")
        except ValueError:
            return None
        parsed = urlparse(url)
        if not parsed.hostname:
            return None
        host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        return f"{parsed.scheme.lower()}://{host}" + (f":{parsed.port}" if parsed.port else "") + (parsed.path or "")

    async def read_state(self, url: str, token: str, entity_id: str, *, timeout: float | None = None) -> dict:
        """One entity's state object, for an explicit (already frozen) connection.

        Raises instead of answering None: the caller tells a timeout from a missing entity from an
        unreachable server. Nothing is logged here -- a transport error's text names the URL.
        """
        safe_url = self._validate_url(url)
        if not safe_url:
            raise ValueError("Invalid Home Assistant URL")
        async with httpx.AsyncClient(timeout=self.timeout if timeout is None else timeout) as client:
            response = await client.get(
                f"{safe_url.rstrip('/')}/api/states/{entity_id}",
                headers={"Authorization": f"Bearer {token}"},
            )
            response.raise_for_status()
            return response.json()

    async def test_connection(self, url: str, token: str) -> dict:
        """Test connection to Home Assistant.

        Returns dict with:
            - success: bool
            - message: str or None (HA message on success)
            - reason: a code of ``HA_TEST_FAILURES`` on failure, else None
            - error: that code's sentence on failure, else None

        TLS is verified and no redirect is followed, exactly as for every other call of this
        service; a failure only gets its name. The reason is logged with the destination's host
        alone — never the token, the address's user info or path, or an exception's text.
        """

        def failed(reason: str) -> dict:
            logger.warning(
                "Home Assistant connection test failed: reason=%s destination=%s", reason, _log_destination(url)
            )
            return {"success": False, "message": None, "reason": reason, "error": HA_TEST_FAILURES[reason]}

        safe_url = self._validate_url(url)
        if not safe_url:
            return failed("invalid_url")
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{safe_url.rstrip('/')}/api/",
                    headers={"Authorization": f"Bearer {token}"},
                )
        except httpx.TimeoutException:
            return failed("timeout")
        except httpx.InvalidURL:
            return failed("invalid_url")
        except Exception as e:
            reason = tls_failure(e)
            if reason is None:
                # Refused, unresolved, reset, a protocol the server does not speak: nothing usable answered.
                reason = "unreachable" if isinstance(e, httpx.TransportError) else "unexpected_answer"
            return failed(reason)
        status = response.status_code
        if status in (401, 403):
            return failed("unauthorized")
        if 300 <= status < 400:
            return failed("redirect")
        if not 200 <= status < 300:
            return failed("unexpected_answer")
        try:
            data = response.json()
        except ValueError:
            data = None
        # Home Assistant's API root answers {"message": "API running."}; any other 2xx is some
        # other web server at this address.
        if not isinstance(data, dict) or not isinstance(data.get("message"), str):
            return failed("unexpected_answer")
        return {"success": True, "message": data["message"], "reason": None, "error": None}

    async def list_entities(self, url: str, token: str, search: str | None = None) -> list[dict]:
        """List available entities from HA.

        Always filters to switch/light/input_boolean/script — the only domains
        the SmartPlugBase.ha_entity_id pattern accepts. When a search query is
        provided it narrows the same domain-filtered list by entity_id or
        friendly_name substring (case-insensitive). Whitespace-only search
        strings fall back to the no-search behaviour.

        Previously search bypassed the domain filter, which let users pick a
        sensor.* or binary_sensor.* entity from the dropdown that the backend
        schema would then reject with the cryptic Pydantic pattern error.
        Picking what you can't save isn't a useful UX — upstream Bambuddy
        #1388 / commit 135b8fd9.

        Returns list of entity dicts with:
            - entity_id: str
            - friendly_name: str
            - state: str
            - domain: str
        """
        # Allowed domains for smart plug control — must mirror the regex in
        # backend/app/schemas/smart_plug.py (SmartPlugBase.ha_entity_id).
        allowed_domains = {"switch", "light", "input_boolean", "script"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{url.rstrip('/')}/api/states",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()

                entities = []
                search_lower = search.lower().strip() if search else None

                for entity in response.json():
                    entity_id = entity.get("entity_id", "")
                    domain = entity_id.split(".")[0] if "." in entity_id else ""
                    friendly_name = entity.get("attributes", {}).get("friendly_name", entity_id)

                    if domain not in allowed_domains:
                        continue

                    if search_lower and (
                        search_lower not in entity_id.lower() and search_lower not in friendly_name.lower()
                    ):
                        continue

                    entities.append(
                        {
                            "entity_id": entity_id,
                            "friendly_name": friendly_name,
                            "state": entity.get("state"),
                            "domain": domain,
                        }
                    )

                return sorted(entities, key=lambda x: x["friendly_name"].lower())
        except Exception as e:
            logger.warning("Failed to list HA entities: %s", e)
            return []

    async def list_sensor_entities(self, url: str, token: str) -> list[dict]:
        """List available sensor entities for energy monitoring.

        Returns list of sensor entities with power/energy units.
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    f"{url.rstrip('/')}/api/states",
                    headers={"Authorization": f"Bearer {token}"},
                )
                response.raise_for_status()

                # Valid units for energy monitoring sensors (lowercase for case-insensitive matching)
                power_units = {"w", "kw", "mw"}
                energy_units = {"kwh", "wh", "mwh"}
                valid_units = power_units | energy_units

                entities = []
                for entity in response.json():
                    entity_id = entity.get("entity_id", "")
                    domain = entity_id.split(".")[0] if "." in entity_id else ""

                    # Filter to sensor domain only
                    if domain != "sensor":
                        continue

                    attrs = entity.get("attributes", {})
                    unit = attrs.get("unit_of_measurement", "")

                    # Only include sensors with power/energy units (case-insensitive)
                    if unit.lower() in valid_units:
                        entities.append(
                            {
                                "entity_id": entity_id,
                                "friendly_name": attrs.get("friendly_name", entity_id),
                                "state": entity.get("state"),
                                "unit_of_measurement": unit,
                            }
                        )

                return sorted(entities, key=lambda x: x["friendly_name"].lower())
        except Exception as e:
            logger.warning("Failed to list HA sensor entities: %s", e)
            return []

    async def list_display_entities(self, url: str, token: str, search: str | None = None) -> list[dict]:
        """Discover binary entities and numeric HA sensors, including unitless ones."""
        safe_url = self._validate_url(url)
        if safe_url is None:
            raise ValueError("Invalid Home Assistant URL")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.get(
                f"{safe_url.rstrip('/')}/api/states", headers={"Authorization": f"Bearer {token}"}
            )
            response.raise_for_status()
            entities = []
            search_lower = search.strip().lower() if search else ""
            for entity in response.json():
                entity_id = entity.get("entity_id", "")
                domain = entity_id.split(".", 1)[0]
                if domain not in ("binary_sensor", "sensor"):
                    continue
                attrs = entity.get("attributes") or {}
                unit = attrs.get("unit_of_measurement")
                state = entity.get("state")
                if domain == "sensor" and not unit and as_float(state) is None:
                    continue
                name = attrs.get("friendly_name") or entity_id
                if search_lower and search_lower not in entity_id.lower() and search_lower not in name.lower():
                    continue
                entities.append(
                    {
                        "entity_id": entity_id,
                        "friendly_name": name,
                        "state": state,
                        "domain": domain,
                        "device_class": attrs.get("device_class"),
                        "unit_of_measurement": unit,
                    }
                )
            return sorted(entities, key=lambda item: item["friendly_name"].lower())

    async def fetch_states(self, entity_ids: list[str]) -> dict[str, dict | None]:
        """Fetch only bound entities with bounded concurrency and config isolation."""
        unique_ids = list(dict.fromkeys(entity_ids))
        if not unique_ids:
            return {}
        base_url, token, generation = self.base_url, self.token, self._config_generation
        if not base_url or not token:
            return dict.fromkeys(unique_ids)
        semaphore = asyncio.Semaphore(8)
        async with httpx.AsyncClient(timeout=self.timeout) as client:

            async def fetch_one(entity_id: str) -> tuple[str, dict | None]:
                async with semaphore:
                    try:
                        response = await client.get(
                            f"{base_url}/api/states/{entity_id}", headers={"Authorization": f"Bearer {token}"}
                        )
                        response.raise_for_status()
                        return entity_id, response.json()
                    except httpx.HTTPError as exc:
                        logger.debug("HA state read failed for %s: %s", entity_id, exc)
                        return entity_id, None

            results = await asyncio.gather(*(fetch_one(entity_id) for entity_id in unique_ids))
        if generation != self._config_generation:
            return dict.fromkeys(unique_ids)
        return dict(results)


def as_float(value) -> float | None:
    """Only finite HA numeric states are readings or alert operands."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# Singleton instance
homeassistant_service = HomeAssistantService()
