from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, ValidationError, computed_field, model_validator

from backend.app.schemas.not_null import IgnoresNullForRequiredColumns

SMART_PLUG_SECRET_FIELDS = (
    "password",
    "rest_headers",
    "rest_on_url",
    "rest_off_url",
    "rest_status_url",
    "rest_power_url",
    "rest_energy_url",
    "rest_on_body",
    "rest_off_body",
)

PLUG_TYPES = ("tasmota", "homeassistant", "mqtt", "rest", "zigbee")

# What a plug of each type cannot work without, as machine codes with the sentence a refused write
# carries. ONE rule with two callers: a write refuses the first problem (``validate_plug_type_fields``,
# which the PATCH resolver also calls on the merged row), a read reports them all on the row
# (``SmartPlugResponse.problems``) instead of failing the whole answer.
_PLUG_TYPE_PROBLEMS = {
    "unknown_plug_type": "Unknown smart plug type",
    "ip_address_required": "ip_address is required for Tasmota plugs",
    "ha_entity_required": "ha_entity_id is required for Home Assistant plugs",
    "mqtt_topic_required": "At least one MQTT topic must be configured for power, energy, or state monitoring",
    "rest_url_required": "At least one of ON URL or OFF URL is required for REST plugs",
    "zigbee_address_required": "A Zigbee plug needs the IEEE address of a paired device",
}


def plug_type_problems(plug) -> list[str]:
    """The cross-field problems of a plug, read by attribute: a schema object, an ORM row or a stand-in."""

    def field(name):
        return getattr(plug, name, None)

    kind = field("plug_type")
    if kind not in PLUG_TYPES:
        return ["unknown_plug_type"]
    problems = []
    if kind == "tasmota" and not field("ip_address"):
        problems.append("ip_address_required")
    if kind == "homeassistant" and not field("ha_entity_id"):
        problems.append("ha_entity_required")
    # MQTT: the new power topic takes priority over the legacy one, and a path is optional (without
    # one the raw payload is the value), so one topic of any kind is a data source.
    if kind == "mqtt" and not any(
        field(name) for name in ("mqtt_power_topic", "mqtt_topic", "mqtt_energy_topic", "mqtt_state_topic")
    ):
        problems.append("mqtt_topic_required")
    if kind == "rest" and field("enabled") and not field("rest_on_url") and not field("rest_off_url"):
        problems.append("rest_url_required")
    if kind == "zigbee" and not field("zigbee_ieee"):
        # Only the shape is checked here. Whether the address is actually on the mesh is a question
        # for the route, which can see the coordinator; a schema cannot.
        problems.append("zigbee_address_required")
    return problems


class SmartPlugBase(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    plug_type: Literal["tasmota", "homeassistant", "mqtt", "rest", "zigbee"] = "tasmota"

    # Zigbee fields (required when plug_type="zigbee", m115)
    #
    # No scaling fields alongside it, unlike MQTT and REST: a Zigbee plug
    # reports its own multiplier/divisor on the Metering cluster, so the device
    # says what its counter means instead of the operator having to.
    zigbee_ieee: str | None = None

    # Tasmota fields (required when plug_type="tasmota")
    ip_address: str | None = Field(default=None, pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    username: str | None = None
    password: str | None = None

    # Home Assistant fields (required when plug_type="homeassistant")
    ha_entity_id: str | None = Field(default=None, pattern=r"^(switch|light|input_boolean|script)\.[a-z0-9_]+$")
    # Home Assistant energy sensor entities (optional, for separate energy sensors)
    ha_power_entity: str | None = Field(default=None, pattern=r"^sensor\.[a-z0-9_]+$")
    ha_energy_today_entity: str | None = Field(default=None, pattern=r"^sensor\.[a-z0-9_]+$")
    ha_energy_total_entity: str | None = Field(default=None, pattern=r"^sensor\.[a-z0-9_]+$")

    # MQTT fields (required when plug_type="mqtt")
    # Legacy field - kept for backward compatibility
    mqtt_topic: str | None = Field(default=None, max_length=200)  # Deprecated, use mqtt_power_topic

    # Power monitoring
    mqtt_power_topic: str | None = Field(default=None, max_length=200)  # Topic for power data
    mqtt_power_path: str | None = Field(default=None, max_length=100)  # e.g., "power_l1" or "data.power"
    mqtt_power_multiplier: float = Field(default=1.0, gt=0, le=10000)  # Unit conversion for power

    # Energy monitoring
    mqtt_energy_topic: str | None = Field(default=None, max_length=200)  # Topic for energy data
    mqtt_energy_path: str | None = Field(default=None, max_length=100)  # e.g., "energy_l1"
    mqtt_energy_multiplier: float = Field(default=1.0, gt=0, le=10000)  # Unit conversion for energy

    # State monitoring
    mqtt_state_topic: str | None = Field(default=None, max_length=200)  # Topic for state data
    mqtt_state_path: str | None = Field(default=None, max_length=100)  # e.g., "state_l1" for ON/OFF
    mqtt_state_on_value: str | None = Field(
        default=None, max_length=50
    )  # What value means "ON" (e.g., "ON", "true", "1")

    # Lifetime energy source (mirrors rest_energy_total_*). Only this figure may
    # feed energy snapshots; see the model for why it is a separate path rather
    # than a flag on mqtt_energy_path.
    mqtt_energy_total_topic: str | None = Field(default=None, max_length=200)
    mqtt_energy_total_path: str | None = Field(default=None, max_length=100)  # e.g., "energy"
    mqtt_energy_total_multiplier: float = Field(default=1.0, gt=0, le=10000)

    # Control - absent means monitor-only, which is valid configuration
    mqtt_command_topic: str | None = Field(default=None, max_length=200)
    mqtt_command_on: str | None = Field(default=None)  # e.g., '{"state": "ON"}'
    mqtt_command_off: str | None = Field(default=None)

    # Legacy multiplier - kept for backward compatibility
    mqtt_multiplier: float = Field(default=1.0, gt=0, le=10000)  # Deprecated, use mqtt_power_multiplier

    # REST/Webhook fields (required when plug_type="rest")
    rest_on_url: str | None = Field(default=None, max_length=500)
    rest_on_body: str | None = None
    rest_off_url: str | None = Field(default=None, max_length=500)
    rest_off_body: str | None = None
    rest_method: Literal["GET", "POST", "PUT", "PATCH"] | None = None
    rest_headers: str | None = None  # JSON string of custom headers
    rest_status_url: str | None = Field(default=None, max_length=500)
    rest_status_path: str | None = Field(default=None, max_length=200)
    rest_status_on_value: str | None = Field(default=None, max_length=50)
    rest_power_url: str | None = Field(default=None, max_length=500)
    rest_power_path: str | None = Field(default=None, max_length=200)
    rest_power_multiplier: float = Field(default=1.0, gt=0, le=10000)
    rest_energy_url: str | None = Field(default=None, max_length=500)
    rest_energy_path: str | None = Field(default=None, max_length=200)
    rest_energy_multiplier: float = Field(default=1.0, gt=0, le=10000)
    # Lifetime (cumulative) counter — separate from the "today" path above because a
    # given endpoint may expose either, both, or neither. Feeds the hourly energy
    # snapshots and per-print energy, both of which need a monotonic total.
    rest_energy_total_path: str | None = Field(default=None, max_length=200)
    rest_energy_total_multiplier: float = Field(default=1.0, gt=0, le=10000)

    printer_id: int | None = None
    # False for accessory plugs (filter / light / dryer) sharing a printer, so
    # switching one off doesn't mark the printer offline (#2629).
    controls_printer_power: bool = True
    enabled: bool = True
    auto_on: bool = True
    auto_off: bool = True
    auto_off_persistent: bool = False
    off_delay_mode: Literal["time", "temperature"] = "time"
    off_delay_minutes: int = Field(default=5, ge=0, le=60)
    off_temp_threshold: int = Field(default=70, ge=30, le=150)
    # #1349: auto-off after AMS drying completes. Independent of
    # ``auto_off`` (print-finish). Fires whenever any AMS on the linked
    # printer finishes a dry cycle.
    auto_off_after_drying: bool = False
    off_delay_after_drying_minutes: int = Field(default=10, ge=0, le=120)
    # Power alerts
    power_alert_enabled: bool = False
    power_alert_high: float | None = Field(default=None, ge=0, le=5000)  # Alert when power > this (watts)
    power_alert_low: float | None = Field(default=None, ge=0, le=5000)  # Alert when power < this (watts)
    # Schedule
    schedule_enabled: bool = False
    schedule_on_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # HH:MM format
    schedule_off_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")  # HH:MM format
    # Visibility options
    show_in_switchbar: bool = False
    show_on_printer_card: bool = True  # For scripts: show on printer card
    # Meter watch (spec plug-meter-watchdog r4). How often an MQTT plug publishes its watts (NULL = 300 s);
    # the energy a flat counter must have missed before it is suspected (Wh, NULL = 100); whether the
    # firmware may repeat watts after its relay opens (NULL = the type's default: Zigbee yes).
    mqtt_expected_power_interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    meter_min_expected_wh: float | None = Field(default=None, ge=100, le=1_000_000)
    phantom_watts_risk: bool | None = None

    @model_validator(mode="after")
    def validate_plug_type_fields(self) -> "SmartPlugBase":
        problems = plug_type_problems(self)
        if problems:
            raise ValueError(_PLUG_TYPE_PROBLEMS[problems[0]])
        return self


class SmartPlugCreate(SmartPlugBase):
    secret_actions: dict[str, str] | None = None


class SmartPlugUpdate(IgnoresNullForRequiredColumns):
    __orm_table__ = "smart_plugs"

    secret_actions: dict[str, str] | None = None
    name: str | None = None
    plug_type: Literal["tasmota", "homeassistant", "mqtt", "rest", "zigbee"] | None = None
    zigbee_ieee: str | None = None
    ip_address: str | None = None
    ha_entity_id: str | None = None
    # Home Assistant energy sensor entities (optional)
    ha_power_entity: str | None = None
    ha_energy_today_entity: str | None = None
    ha_energy_total_entity: str | None = None
    # MQTT fields (legacy)
    mqtt_topic: str | None = None
    mqtt_multiplier: float | None = Field(default=None, gt=0, le=10000)
    # MQTT power fields
    mqtt_power_topic: str | None = None
    mqtt_power_path: str | None = None
    mqtt_power_multiplier: float | None = Field(default=None, gt=0, le=10000)
    # MQTT energy fields
    mqtt_energy_topic: str | None = None
    mqtt_energy_path: str | None = None
    mqtt_energy_multiplier: float | None = Field(default=None, gt=0, le=10000)
    # MQTT state fields
    mqtt_state_topic: str | None = None
    mqtt_state_path: str | None = None
    mqtt_state_on_value: str | None = None
    # MQTT lifetime energy fields
    mqtt_energy_total_topic: str | None = None
    mqtt_energy_total_path: str | None = None
    mqtt_energy_total_multiplier: float | None = Field(default=None, gt=0, le=10000)
    # MQTT control fields
    mqtt_command_topic: str | None = None
    mqtt_command_on: str | None = None
    mqtt_command_off: str | None = None
    # REST fields
    rest_on_url: str | None = None
    rest_on_body: str | None = None
    rest_off_url: str | None = None
    rest_off_body: str | None = None
    rest_method: Literal["GET", "POST", "PUT", "PATCH"] | None = None
    rest_headers: str | None = None
    rest_status_url: str | None = None
    rest_status_path: str | None = None
    rest_status_on_value: str | None = None
    rest_power_url: str | None = None
    rest_power_path: str | None = None
    rest_power_multiplier: float | None = Field(default=None, gt=0, le=10000)
    rest_energy_url: str | None = None
    rest_energy_path: str | None = None
    rest_energy_multiplier: float | None = Field(default=None, gt=0, le=10000)
    rest_energy_total_path: str | None = None
    rest_energy_total_multiplier: float | None = Field(default=None, gt=0, le=10000)
    printer_id: int | None = None
    controls_printer_power: bool | None = None
    enabled: bool | None = None
    auto_on: bool | None = None
    auto_off: bool | None = None
    auto_off_persistent: bool | None = None
    off_delay_mode: Literal["time", "temperature"] | None = None
    off_delay_minutes: int | None = Field(default=None, ge=0, le=60)
    off_temp_threshold: int | None = Field(default=None, ge=30, le=150)
    # #1349: per-plug drying auto-off.
    auto_off_after_drying: bool | None = None
    off_delay_after_drying_minutes: int | None = Field(default=None, ge=0, le=120)
    username: str | None = None
    password: str | None = None
    # Power alerts
    power_alert_enabled: bool | None = None
    power_alert_high: float | None = Field(default=None, ge=0, le=5000)
    power_alert_low: float | None = Field(default=None, ge=0, le=5000)
    # Schedule
    schedule_enabled: bool | None = None
    schedule_on_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    schedule_off_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    # Visibility options
    show_in_switchbar: bool | None = None
    show_on_printer_card: bool | None = None
    # Meter watch (spec plug-meter-watchdog r4)
    mqtt_expected_power_interval_seconds: int | None = Field(default=None, ge=1, le=3600)
    meter_min_expected_wh: float | None = Field(default=None, ge=100, le=1_000_000)
    phantom_watts_risk: bool | None = None


class SmartPlugResponse(SmartPlugBase):
    # Reads must also serialize values accepted by historical PATCH writers.
    # New/changed values are checked against SmartPlugBase by the write resolver.
    name: str
    ip_address: str | None = None
    ha_entity_id: str | None = None
    ha_power_entity: str | None = None
    ha_energy_today_entity: str | None = None
    ha_energy_total_entity: str | None = None
    mqtt_topic: str | None = None
    mqtt_power_topic: str | None = None
    mqtt_power_path: str | None = None
    mqtt_energy_topic: str | None = None
    mqtt_energy_path: str | None = None
    mqtt_state_topic: str | None = None
    mqtt_state_path: str | None = None
    mqtt_state_on_value: str | None = None
    mqtt_energy_total_topic: str | None = None
    mqtt_energy_total_path: str | None = None
    mqtt_command_topic: str | None = None
    rest_status_path: str | None = None
    rest_status_on_value: str | None = None
    rest_power_path: str | None = None
    rest_energy_path: str | None = None
    rest_energy_total_path: str | None = None
    password: str | None = Field(default=None, exclude=True, repr=False)
    rest_headers: str | None = Field(default=None, exclude=True, repr=False)
    rest_on_url: str | None = Field(default=None, exclude=True, repr=False)
    rest_off_url: str | None = Field(default=None, exclude=True, repr=False)
    rest_status_url: str | None = Field(default=None, exclude=True, repr=False)
    rest_power_url: str | None = Field(default=None, exclude=True, repr=False)
    rest_energy_url: str | None = Field(default=None, exclude=True, repr=False)
    rest_on_body: str | None = Field(default=None, exclude=True, repr=False)
    rest_off_body: str | None = Field(default=None, exclude=True, repr=False)

    @computed_field
    @property
    def configured_secrets(self) -> dict[str, bool]:
        return {field: bool(getattr(self, field)) for field in SMART_PLUG_SECRET_FIELDS}

    # A stored row a write would refuse today is SHOWN and marked, never a failed answer (owner,
    # 2026-11-18): one such row used to fail the whole list and the printer cards' batch with a 500,
    # which also took away the only screen it could be corrected on. ``problems`` holds the codes of
    # ``plug_type_problems`` — the rule the write path refuses by — plus ``invalid_value`` for a row
    # whose stored field no longer fits its type (see ``of``).
    problems: list[str] = []

    @computed_field
    @property
    def needs_attention(self) -> bool:
        return bool(self.problems)

    @model_validator(mode="after")
    def validate_plug_type_fields(self) -> "SmartPlugResponse":
        """Overrides the write-time validator by name: a read reports the problems, it never raises."""
        found = plug_type_problems(self)
        self.problems = found + [code for code in self.problems if code not in found]
        return self

    @classmethod
    def of(cls, plug) -> "SmartPlugResponse":
        """The answer for one stored row, whatever state it was saved in."""
        try:
            return cls.model_validate(plug, from_attributes=True)
        except ValidationError:
            # A field no longer fits its declared range or type. The row is still shown as stored,
            # unvalidated, so it can be found and corrected.
            values = {name: getattr(plug, name) for name in cls.model_fields if hasattr(plug, name)}
            values["problems"] = [*plug_type_problems(plug), "invalid_value"]
            return cls.model_construct(**values)

    id: int
    # ⚠️ Whether this is the plug the printer card treats as the machine's power
    # — computed by ``_main_plug_rank``, never by the client. Anything that
    # switches a printer on or off must agree with the card's own Power row, and
    # a second ranking written in TypeScript is a second answer waiting to
    # drift. False on every plug with no printer.
    is_main_plug: bool = False
    last_state: str | None = None
    last_checked: datetime | None = None
    auto_off_executed: bool = False  # True when auto-off was triggered after print
    power_alert_last_triggered: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class MeterIncidentOut(BaseModel):
    """An active suspicion that a plug's lifetime counter stopped following its load (spec W6)."""

    plug_id: int
    opened_at: datetime
    flat_since: datetime
    flat_until: datetime
    total_kwh: float
    evidence: dict | None = None

    class Config:
        from_attributes = True


class SmartPlugControl(BaseModel):
    action: Literal["on", "off", "toggle"]


class SmartPlugEnergy(BaseModel):
    """Energy monitoring data from a smart plug."""

    power: float | None = None  # Current watts
    voltage: float | None = None  # Volts
    current: float | None = None  # Amps
    today: float | None = None  # kWh used today
    yesterday: float | None = None  # kWh used yesterday
    total: float | None = None  # Total kWh
    factor: float | None = None  # Power factor (0-1)
    apparent_power: float | None = None  # VA
    reactive_power: float | None = None  # VAr


class SmartPlugStatus(BaseModel):
    state: str | None = None  # "ON", "OFF", or None if unreachable
    reachable: bool = True
    device_name: str | None = None
    energy: SmartPlugEnergy | None = None  # Energy data if available


class SmartPlugTestConnection(BaseModel):
    plug_id: int | None = None
    secret_actions: dict[str, str] | None = None
    ip_address: str = Field(..., pattern=r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")
    username: str | None = None
    password: str | None = None


# Home Assistant schemas
class HATestConnectionRequest(BaseModel):
    """Request to test Home Assistant connection."""

    url: str | None = Field(default=None, min_length=1)
    token: str | None = Field(default=None, min_length=1, repr=False)
    secret_actions: dict[str, str] | None = None


class HATestConnectionResponse(BaseModel):
    """Response from HA connection test."""

    success: bool
    message: str | None = None
    # On failure: why, as a code of ``homeassistant.HA_TEST_FAILURES`` — what the interface reacts
    # to and translates — and that code's English sentence.
    reason: str | None = None
    error: str | None = None


class HAEntity(BaseModel):
    """A Home Assistant entity that can be used as a smart plug."""

    entity_id: str
    friendly_name: str
    state: str | None = None
    domain: str  # "switch", "light", "input_boolean", "script"


class HASensorEntity(BaseModel):
    """A Home Assistant sensor entity for energy monitoring."""

    entity_id: str
    friendly_name: str
    state: str | None = None
    unit_of_measurement: str | None = None  # "W", "kW", "kWh", "Wh"


class RESTTestConnectionRequest(BaseModel):
    """Request to test a REST smart plug connection."""

    model_config = {"extra": "forbid"}

    url: str | None = Field(default=None, min_length=1)
    method: str = Field(default="GET")
    headers: str | None = None  # JSON string of custom headers
    plug_id: int | None = None
    draft: SmartPlugUpdate | None = None
    endpoint: Literal["rest_on_url", "rest_off_url", "rest_status_url", "rest_power_url", "rest_energy_url"] = (
        "rest_on_url"
    )


class RESTTestConnectionResponse(BaseModel):
    """Response from REST connection test."""

    success: bool
    error: str | None = None
