import json
import re

from pydantic import BaseModel, Field, field_validator

# Outbound service URLs validated when they are saved, so a bad value is rejected
# at configuration time with a message naming the field rather than failing
# opaquely at request time. All of these are commonly self-hosted on the same
# host or LAN as BamDude, so the LAN-service policy applies — see
# ``api/routes/_url_safety.assert_safe_lan_service_url``.
#
# Module-level rather than a class attribute so the drift-guard test can import
# the real tuple and cannot fall out of step with it. **Any new outbound-URL
# setting belongs here** — or, if it must be reachable on the public internet, on
# the stricter OIDC guard instead.
LAN_SERVICE_URL_SETTINGS = ("ha_url", "obico_ml_url", "orcaslicer_api_url", "bambu_studio_api_url")

# ⚠️ ``docker_compose_dir`` is unusual among the string settings: BamDude never
# consumes it. It is interpolated into a shell command that the Settings page
# invites the operator to copy and paste into a root-capable terminal. A value
# like ``/opt/bamdude; rm -rf /`` would render as a perfectly plausible-looking
# update command, so anyone holding ``settings:update`` could hand every admin a
# destructive one-liner to run. Restricting it to characters that occur in real
# paths removes that entirely.
_COMPOSE_DIR_ALLOWED = re.compile(r"^[\w \-./\\:~]+$")
_COMPOSE_DIR_MAX_LEN = 512


class AppSettings(BaseModel):
    """Application settings schema."""

    save_thumbnails: bool = Field(default=True, description="Extract and save preview images from 3MF files")
    capture_finish_photo: bool = Field(
        default=True,
        description=(
            "Capture photo from printer camera when print completes. BamDude records a "
            "brief timelapse during the print so the photo can be sourced from the moment "
            "before the bed drops; the timelapse file is kept if you enabled timelapse for "
            "this print, otherwise it is deleted automatically after the photo is captured."
        ),
    )
    # ⚠️ Off by default, and it stays off unless somebody chooses it. "BamDude
    # has a copy" is not the same as "nobody needs it on the machine" — the
    # recording may be there to watch from the printer's own screen or to carry
    # away on the card.
    delete_timelapse_after_attach: bool = Field(
        default=False,
        description=(
            "Delete a timelapse from the printer once it has been attached to its archive. "
            "Only ever after a successful attach, and through the medium it was read from."
        ),
    )
    archive_3mf_retention_enabled: bool = Field(
        default=False,
        description="Auto-delete 3MF files of archive groups whose newest print is older than the retention window",
    )
    archive_3mf_retention_days: int = Field(
        default=30,
        ge=1,
        description="Days since the design's most recent print before its 3MF copies are eligible for cleanup. Minimum 1.",
    )
    # Runout zero-point accounting (usage journal, m153).
    runout_zero_point_enabled: bool = Field(
        default=True,
        description="Close a spool at exactly empty when the printer reports an unambiguous filament runout",
    )
    ams_sync_bidirectional: bool = Field(
        default=True,
        description=(
            "Allow idle AMS readings to correct a Bambu-tagged spool's weight downward "
            "(a value must repeat across two reports a minute apart)"
        ),
    )
    runout_purge_grams: int = Field(
        default=0,
        ge=0,
        le=500,
        description="Grams charged to the backup spool for the purge of an AMS auto-switch (0 = off)",
    )
    usage_events_retention_hours: int = Field(
        default=72,
        ge=1,
        le=8760,
        description="Hours to keep a finished print's usage-journal events (runout/tray timeline) for forensics",
    )
    # 0 = unset. There is no sensible default price of plastic, and a
    # plausible figure reads as an answer while a blank reads as a blank.
    default_filament_cost: float = Field(default=0.0, description="Default filament cost per kg (0 = unset)")
    currency: str = Field(default="USD", description="Currency for cost tracking")
    energy_cost_per_kwh: float = Field(default=0.15, description="Electricity cost per kWh for energy tracking")

    # Spoolman integration
    spoolman_enabled: bool = Field(default=False, description="Enable Spoolman integration for filament tracking")

    # Zigbee coordinator (phase 1). Declared here rather than left as loose
    # key-value rows because ``update_settings`` persists exactly the fields
    # this schema declares — an undeclared key is silently dropped by Pydantic,
    # so without these there is no way to configure Zigbee short of writing to
    # the database by hand. The phase-4 UI binds to the same three.
    zigbee_enabled: bool = Field(default=False, description="Run the built-in Zigbee coordinator")
    # Off by default: it needs a desktop bridge running somewhere, so a farm
    # that has none must not be shown a queue nothing will ever drain.
    device_labels_enabled: bool = Field(
        default=False,
        description="Print spool labels directly on a printer attached to a desktop bridge",
    )
    zigbee_transport: str = Field(default="ethernet", description="Zigbee dongle transport: ethernet or usb")
    zigbee_path: str = Field(
        default="",
        description="Zigbee dongle address: host:port for ethernet, serial device path for usb",
    )
    spoolman_url: str = Field(default="", description="Spoolman server URL (e.g., http://localhost:7912)")
    spoolman_sync_mode: str = Field(
        default="auto", description="Sync mode: 'auto' syncs immediately, 'manual' requires button press"
    )
    spoolman_disable_weight_sync: bool = Field(
        default=False,
        description="Disable remaining_weight sync. When enabled, only location is updated for existing spools.",
    )
    spoolman_report_partial_usage: bool = Field(
        default=True,
        description="Report Partial Usage for Failed Prints. When a print fails or is cancelled, report the estimated filament used up to that point based on layer progress.",
    )
    auto_add_unknown_rfid: bool = Field(
        default=True,
        description="Automatically add spools with unknown RFID tags to inventory. Disable if you pre-create inventory entries manually to avoid duplicates.",
    )
    disable_filament_warnings: bool = Field(
        default=False,
        description="Disable insufficient filament warnings when printing or queueing prints",
    )

    # Virtual spool display name — composed per-request on the frontend from the
    # Spool's columns + computed fields. Available placeholders: {brand},
    # {material}, {subtype}, {color_name}, {slicer_filament_name}, {note},
    # {label_weight_g}, {label_weight_kg}, {remaining_g}, {remaining_kg},
    # {remaining_pct}, {color_hex}, {cost_per_kg}. Unknown placeholders are
    # kept verbatim so typos surface instead of silently collapsing. Used by
    # the Filaments page for tokenised substring search and sort-by-name.
    spool_display_template: str = Field(
        default="{brand} {material} {color_name}",
        description="Template for the synthesised spool display name",
    )

    # Updates
    check_updates: bool = Field(default=True, description="Automatically check for updates on startup")
    check_printer_firmware: bool = Field(default=True, description="Check for printer firmware updates from Bambu Lab")
    include_beta_updates: bool = Field(default=False, description="Include beta/prerelease versions in update checks")
    telemetry_enabled: bool = Field(
        default=True, description="Send anonymized usage telemetry (opt-out; no PII, no IPs, no serials)"
    )

    # Language
    language: str = Field(default="en", description="UI language (en, de, fr, ja, it, pt-BR)")

    # Telegram
    telegram_registration_open: bool = Field(
        default=False, description="Allow unknown Telegram chats to auto-register (pending admin activation)"
    )

    # Bed cooled notification threshold
    bed_cooled_threshold: float = Field(
        default=35.0, description="Bed temperature threshold for cooled notification (°C)"
    )

    # AMS threshold settings for humidity and temperature coloring
    ams_humidity_good: int = Field(default=40, description="Humidity threshold for good (green): <= this value")
    ams_humidity_fair: int = Field(
        default=60, description="Humidity threshold for fair (orange): <= this value, > is red"
    )
    ams_temp_good: float = Field(default=28.0, description="Temperature threshold for good (blue): <= this value")
    ams_temp_fair: float = Field(
        default=35.0, description="Temperature threshold for fair (orange): <= this value, > is red"
    )
    ams_history_retention_days: int = Field(default=30, description="Number of days to keep AMS sensor history data")
    plug_power_history_retention_days: int = Field(
        default=30, description="Number of days to keep smart-plug power history"
    )
    sensor_history_retention_days: int = Field(
        default=30, description="Number of days to keep sensor measurement history"
    )
    plug_power_sample_seconds: int = Field(
        default=60, description="How often plugs that do not report on their own are read for history"
    )
    printer_sensor_history_retention_days: int = Field(
        default=30, description="Number of days to keep printer heater (nozzle/bed/chamber) history data"
    )
    log_retention_days: int = Field(
        default=7,
        ge=1,
        le=365,
        description="Number of days to keep historical daily log archives (bamdude-YYYY-MM-DD.log).",
    )

    # Queue auto-drying settings
    queue_drying_enabled: bool = Field(
        default=False, description="Automatically dry AMS filament between queued prints"
    )
    prefer_lowest_filament: bool = Field(
        default=False,
        description="When multiple AMS trays match, prefer the one with lowest remaining filament",
    )
    queue_shortest_first: bool = Field(
        default=False,
        description="Auto-queue: prefer shorter print jobs first (with been_jumped starvation guard)",
    )
    # Preheat / heat-soak before queued prints (#1468). The scheduler stage runs on the
    # idle printer between FTP upload and start_print. Three hardware tiers: chamber heater
    # (H2C/H2D/H2D Pro/H2S/X2D/X1E) set_ctt → wait for chamber sensor → soak; chamber sensor
    # only (X1C/P2S) M140 → wait for radiant warm-up OR timeout → soak; no chamber sensor
    # (P1S/P1P/A1/A1 Mini) M140 → fixed soak timer. Chamber target derives per-print from
    # the loaded AMS filament types (max across slots); 0 skips the chamber phase but keeps
    # the bed phase + soak. Per-item preheat_chamber_target_override bypasses the derivation.
    preheat_enabled: bool = Field(
        default=False,
        description="Master toggle / default for new queue items. Per-item preheat_override can flip the decision per print.",
    )
    preheat_filament_targets: str = Field(
        default="",
        description=(
            "JSON map of normalized filament type -> chamber target degC. Empty = bundled defaults "
            "(PLA/PETG/TPU/PVA: 0, PETG-CF: 40, ABS/ASA: 45, PA/PC/PC-FR: 50, PA-CF: 55, default: 0). "
            "Scheduler picks the max across loaded AMS slots; 0 disables the chamber phase for that print."
        ),
    )
    preheat_max_wait_seconds: int = Field(
        default=900,
        ge=60,
        le=3600,
        description="Max time to wait for the chamber to reach target before falling through to soak (radiant heating on X1C/P2S can take 15-30 min).",
    )
    preheat_soak_seconds: int = Field(
        default=300,
        ge=0,
        le=1800,
        description="Hold time at temperature after the chamber reaches target (or after max_wait elapses). 0 = no soak.",
    )
    queue_drying_block: bool = Field(
        default=False,
        description="Block queue until drying completes (when disabled, prints take priority over drying)",
    )
    ambient_drying_enabled: bool = Field(
        default=False,
        description="Automatically dry AMS filament on idle printers when humidity exceeds threshold, regardless of queue",
    )
    print_drying_enabled: bool = Field(
        default=False,
        description=(
            "Allow auto-drying to also fire on a printer that is currently printing, "
            "when its model+firmware supports concurrent drying (H2D 01.03.00.00+, "
            "H2C/H2S/P2S/H2D Pro 01.02.00.00+, X2D/A2L 01.01.00.00+, X1C 01.11.02.00+). "
            "Drying temperature is automatically capped 5 degC below the idle preset "
            "(floor 40 degC) to protect spools during print."
        ),
    )
    drying_presets: str = Field(
        default="",
        description="JSON blob of drying presets per filament type (empty = use built-in defaults)",
    )
    ams_humidity_thresholds: str = Field(
        default="",
        description=(
            "JSON blob of per-filament-type humidity trigger thresholds for auto-drying and alarms. "
            'Shape: {"default": int, "PLA": int, "ASA": int, ...}. '
            "Empty = fall back to ams_humidity_fair for all types."
        ),
    )
    zigbee_sensor_reporting: str = Field(
        default="",
        description=(
            "JSON blob of Zigbee sensor reporting parameters per measurement. "
            'Shape: {"temperature": {"min_interval": int, "max_interval": int, "reportable_change": float}, ...}. '
            "reportable_change is in the measurement's display unit (°C, %, ppm, µg/m³). "
            "Empty, or any missing field, falls back to the registry defaults."
        ),
    )
    zigbee_sensor_stale_multiplier: str = Field(
        default="2",
        description="A Zigbee sensor reading older than this multiple of its reporting max_interval is stale.",
    )
    zigbee_sensor_poll_seconds: str = Field(
        default="30",
        description=(
            "Poll cadence for MAINS-powered Zigbee sensors, jittered like the plug poller. "
            "Battery sensors are never polled on a timer: they are asleep, and each attempt would "
            "hold the shared radio until it timed out."
        ),
    )

    # Scheduled local backup (upstream #884)
    local_backup_enabled: bool = Field(default=False, description="Enable scheduled local backups")
    local_backup_schedule: str = Field(default="daily", description="Backup frequency: hourly, daily, weekly")
    local_backup_time: str = Field(default="03:00", description="Time of day for daily/weekly backups (HH:MM, 24h)")
    local_backup_retention: int = Field(default=5, description="Number of backup files to keep (1-100)")
    local_backup_path: str = Field(default="", description="Backup output directory (empty = DATA_DIR/backups)")

    # Staggered start settings (electrical load management for farms)
    stagger_enabled: bool = Field(
        default=False, description="Enable staggered start to limit concurrent printer heating"
    )
    stagger_concurrent: int = Field(default=2, description="Max printers that can be heating simultaneously")
    stagger_interval_minutes: int = Field(
        default=5, description="Wait time (minutes) after a slot frees before next start"
    )
    stagger_wait_for_bed: bool = Field(
        default=True,
        description="Slot frees when bed reaches target temp (±1°C). When off, frees immediately after start.",
    )

    # Print modal settings
    per_printer_mapping_expanded: bool = Field(
        default=False, description="Expand custom filament mapping by default in print modal"
    )

    # Date/time display format
    date_format: str = Field(default="system", description="Date format: system, us, eu, iso")
    time_format: str = Field(default="system", description="Time format: system, 12h, 24h")

    # Default printer for operations
    default_printer_id: int | None = Field(default=None, description="Default printer ID for uploads, reprints, etc.")

    # Virtual Printer
    virtual_printer_enabled: bool = Field(default=False, description="Enable virtual printer for slicer uploads")
    virtual_printer_access_code: str = Field(default="", description="Access code for virtual printer authentication")
    virtual_printer_mode: str = Field(
        default="file_manager",
        description="Mode: 'print_queue' (archive + push directly to a per-printer queue), 'auto_queue' (archive + drop into the auto-queue router), 'file_manager' (save to library), or 'proxy' (transparent forward to a real printer)",
    )
    virtual_printer_archive_name_source: str = Field(
        default="metadata",
        description="Source for the archive's display name on virtual-printer uploads: 'metadata' uses the 3MF's embedded print_name (default, matches Bambu's behavior), 'filename' uses the filename Bambu Studio sent over FTP (lets users rename via the slicer's 'send to printer' dialog).",
    )

    # Dark mode theme settings
    dark_style: str = Field(default="classic", description="Dark mode style: classic, glow, vibrant")
    dark_background: str = Field(
        default="neutral", description="Dark mode background: neutral, warm, cool, oled, slate, forest"
    )
    dark_accent: str = Field(default="green", description="Dark mode accent: green, teal, blue, orange, purple, red")

    # Light mode theme settings
    light_style: str = Field(default="classic", description="Light mode style: classic, glow, vibrant")
    light_background: str = Field(default="neutral", description="Light mode background: neutral, warm, cool")
    light_accent: str = Field(default="green", description="Light mode accent: green, teal, blue, orange, purple, red")

    # FTP retry settings for unreliable WiFi connections
    ftp_retry_enabled: bool = Field(default=True, description="Enable automatic retry for FTP operations")
    ftp_retry_count: int = Field(default=3, description="Number of retry attempts for FTP operations (1-10)")
    ftp_retry_delay: int = Field(default=2, description="Seconds to wait between FTP retry attempts (1-30)")
    ftp_timeout: int = Field(default=30, description="FTP connection timeout in seconds (10-300)")

    # MQTT Relay settings for publishing events to external broker
    mqtt_enabled: bool = Field(default=False, description="Enable MQTT event publishing to external broker")
    mqtt_broker: str = Field(default="", description="MQTT broker hostname or IP address")
    mqtt_port: int = Field(default=1883, description="MQTT broker port (default 1883, TLS typically 8883)")
    mqtt_username: str = Field(default="", description="MQTT username for authentication (optional)")
    mqtt_password: str = Field(default="", description="MQTT password for authentication (optional)")
    mqtt_topic_prefix: str = Field(default="bamdude", description="Topic prefix for all published messages")
    mqtt_use_tls: bool = Field(default=False, description="Use TLS/SSL encryption for MQTT connection")

    # External URL for notifications
    # Where the compose file lives on the host, for the update instructions.
    # Never read by BamDude itself — see the validator on the update schema.
    docker_compose_dir: str = Field(
        default="", description="Host directory holding docker-compose.yml, shown in the update command"
    )

    external_url: str = Field(
        default="", description="External URL where BamDude is accessible (for notification images)"
    )

    # Home Assistant integration for smart plug control
    ha_enabled: bool = Field(default=False, description="Enable Home Assistant integration for smart plug control")
    ha_url: str = Field(default="", description="Home Assistant URL (e.g., http://192.168.1.100:8123)")
    ha_token: str = Field(default="", description="Home Assistant Long-Lived Access Token")
    ha_url_from_env: bool = Field(default=False, description="Whether HA URL is set via HA_URL environment variable")
    ha_token_from_env: bool = Field(
        default=False, description="Whether HA token is set via HA_TOKEN environment variable"
    )
    ha_env_managed: bool = Field(
        default=False, description="Whether HA integration is fully managed by environment variables"
    )

    # File Manager / Library settings
    library_disk_warning_gb: float = Field(
        default=5.0,
        description="Show warning when free disk space falls below this threshold (GB)",
    )
    library_all_files_recursive: bool = Field(
        default=False,
        description=(
            "In the File Manager 'All Files' view, list files from every subfolder "
            "recursively. When off (default), 'All Files' shows only root-level files."
        ),
    )
    firmware_batch_concurrency: int = Field(
        default=2,
        ge=1,
        le=10,
        description="Max printers updated in parallel during a bulk firmware run (TLS-handshake budget).",
    )

    # Camera view settings
    camera_view_mode: str = Field(
        default="window",
        description="Camera view mode: 'window' opens in new browser window, 'embedded' shows overlay on main screen",
    )

    # Preferred slicer application (server-side / API sidecar slicer)
    # Where slicing RUNS. ⚠️ Orthogonal to ``preferred_slicer``, which only says
    # which slicer BINARY the sidecar drives: an execution site is not a binary
    # choice. Kept as its own key so the two never have to encode impossible
    # combinations.
    #
    # Only "sidecar" is implemented today; the slice dialog offers a per-job
    # choice once more than one engine exists, and hides the control entirely
    # while there is only one.
    slice_engine: str = Field(
        default="sidecar",
        description="Default execution site for slicing: 'sidecar' (server-side API).",
    )
    preferred_slicer: str = Field(
        default="bambu_studio",
        description="Slicer used for the server-side API / sidecar: 'bambu_studio' or 'orcaslicer'",
    )
    # "Open in Slicer" desktop URI handler — independent of the API slicer so a
    # user can slice via the Bambu Studio sidecar but open files locally in
    # OrcaSlicer, or vice versa (#1329). None falls back to ``preferred_slicer``
    # so existing installs behave identically until someone changes it.
    open_in_slicer: str | None = Field(
        default=None,
        description="Desktop slicer for the 'Open in Slicer' button: 'bambu_studio' or 'orcaslicer'. None inherits from preferred_slicer.",
    )

    # Server-side slicing (B.4) — optional opt-in to route the Slice button
    # through the in-app slicer-api sidecar instead of the OS slicer URI scheme.
    use_slicer_api: bool = Field(
        default=False,
        description=(
            "When true, the Slice button across File Manager / Archives / "
            "MakerWorld dispatches a background job to the configured slicer-api "
            "sidecar. Default off so existing installs see no change until they "
            "explicitly stand up the sidecar Compose stack."
        ),
    )
    orcaslicer_api_url: str = Field(
        default="",
        description=(
            "Base URL of the OrcaSlicer-API sidecar (e.g. http://localhost:3003). "
            "Empty string falls back to the SLICER_API_URL env default."
        ),
    )
    bambu_studio_api_url: str = Field(
        default="",
        description=(
            "Base URL of the BambuStudio-API sidecar (e.g. http://localhost:3001). "
            "Empty string falls back to the BAMBU_STUDIO_API_URL env default."
        ),
    )
    # How long to keep waiting on a slice that is not finishing. Measured
    # against the sidecar's progress channel, not total elapsed time — a heavy
    # model can legitimately slice for half an hour, and a wall-clock ceiling
    # cannot tell that apart from a stalled one (#2730). A sidecar too old to
    # report progress falls back to using this as a total-elapsed ceiling, which
    # is the old behaviour with a number the user can change.
    slicer_stall_timeout_minutes: int = Field(
        default=15,
        ge=1,
        le=240,
        description=(
            "Give up on a slice after this many minutes with no progress from the sidecar. "
            "On a sidecar that does not report progress, applies to total slicing time instead."
        ),
    )

    # Prometheus metrics endpoint
    prometheus_enabled: bool = Field(default=False, description="Enable Prometheus metrics endpoint at /metrics")
    prometheus_token: str = Field(
        default="", description="Bearer token for Prometheus metrics authentication (optional)"
    )

    # Inventory low stock threshold
    low_stock_threshold: float = Field(
        default=20.0,
        ge=0.1,
        le=99.9,
        description="Low stock threshold percentage (%) for inventory filtering and display",
    )

    # Session policy (#1706, adapted) — admin ceiling on effective session lifetime
    # (our refresh-token TTL). Default 720 preserves remember-me (30 d).
    session_max_hours: int = Field(
        default=720,
        ge=1,
        le=720,
        description=(
            "Maximum session lifetime in hours for user logins (default 720 = 30 days, max 720). "
            "Caps how long a login stays valid before re-authentication; applies to new logins and on refresh."
        ),
    )

    # Forecasting (upstream #1184): floor applied on top of each SKU's
    # lead-time. Lets the operator set a uniform default ("everything from
    # this supplier takes at least N days") without touching individual SKUs.
    forecast_global_lead_time_days: int = Field(
        default=0,
        ge=0,
        description="Global minimum lead-time in days; combined with per-SKU lead time as max(global, sku)",
    )

    # Auto-Print G-code Injection (#422). Per-model snippet library:
    # ``{model: {"start_gcode": "...", "end_gcode": "..."}}`` JSON-encoded.
    # Resolved by background_dispatch when a queue item has gcode_injection=True.
    gcode_snippets: str = Field(
        default="",
        description="JSON: per-model G-code injection snippets {model: {start_gcode, end_gcode}}",
    )

    # User email notifications (requires Advanced Authentication)
    user_notifications_enabled: bool = Field(
        default=True,
        description="Enable user email notifications for print job events (requires Advanced Authentication)",
    )

    # Local login (#1589 / G8-H1) — when False, /auth/login rejects username+password
    # (HTTP 401, generic) and the login page hides the credentials form, leaving only
    # OIDC SSO. LDAP has its own ldap_enabled toggle and is unaffected.
    # BAMDUDE_LOCAL_LOGIN=true on the server bypasses this at the route level (recovery).
    local_login_enabled: bool = Field(
        default=True,
        description=(
            "Allow username + password login on /auth/login. Disable when only SSO should be usable. "
            "BAMDUDE_LOCAL_LOGIN=true on the server overrides this to keep a recovery path open."
        ),
    )

    # LDAP authentication
    ldap_enabled: bool = Field(default=False, description="Enable LDAP authentication")
    ldap_server_url: str = Field(default="", description="LDAP server URL (e.g., ldap://ldap.example.com:389)")
    ldap_bind_dn: str = Field(default="", description="Bind DN for LDAP searches (e.g., cn=admin,dc=example,dc=com)")
    ldap_bind_password: str = Field(default="", description="Bind password for LDAP searches")
    ldap_search_base: str = Field(default="", description="Search base DN (e.g., ou=users,dc=example,dc=com)")
    ldap_user_filter: str = Field(
        default="(sAMAccountName={username})",
        description="LDAP user search filter. {username} is replaced with the login username",
    )
    ldap_security: str = Field(default="starttls", description="LDAP security: 'starttls' or 'ldaps'")
    ldap_group_mapping: str = Field(
        default="",
        description="JSON: LDAP group to BamDude group mapping {ldap_group_dn: bamdude_group_name}",
    )
    ldap_auto_provision: bool = Field(
        default=False,
        description="Auto-create BamDude user on first successful LDAP login",
    )
    ldap_default_group: str = Field(
        default="",
        description="Fallback BamDude group name assigned when an LDAP user authenticates but has no mapped groups. Empty = no fallback.",
    )

    # Obico AI failure detection (#172)
    obico_enabled: bool = Field(default=False, description="Enable Obico AI print failure detection")
    obico_ml_url: str = Field(
        default="",
        description="Self-hosted Obico ML API base URL (e.g., http://192.168.1.10:3333)",
    )
    obico_ml_token: str = Field(
        default="",
        description=(
            "Bearer token for the Obico ML API, matching the server's ML_API_TOKEN "
            "environment variable. Empty when the server runs without one."
        ),
    )
    obico_sensitivity: str = Field(
        default="medium",
        description="Detection sensitivity: 'low', 'medium', or 'high' (adjusts LOW/HIGH thresholds)",
    )
    obico_action: str = Field(
        default="notify",
        description="Action on detected failure: 'notify', 'pause', or 'pause_and_off'",
    )
    obico_poll_interval: int = Field(
        default=10,
        ge=5,
        le=120,
        description="Seconds between detection checks while a print is running",
    )
    obico_enabled_printers: str = Field(
        default="",
        description="JSON array of printer IDs to monitor (empty = all connected printers)",
    )

    # Default sidebar order (admin-set for all users)
    default_sidebar_order: str = Field(
        default="",
        description="JSON object with 'order' key containing array of sidebar item IDs (empty = no default)",
    )


class AppSettingsUpdate(BaseModel):
    """Schema for updating settings (all fields optional)."""

    save_thumbnails: bool | None = None
    capture_finish_photo: bool | None = None
    delete_timelapse_after_attach: bool | None = None
    archive_3mf_retention_enabled: bool | None = None
    archive_3mf_retention_days: int | None = Field(default=None, ge=1)
    log_retention_days: int | None = Field(default=None, ge=1, le=365)
    runout_zero_point_enabled: bool | None = None
    ams_sync_bidirectional: bool | None = None
    runout_purge_grams: int | None = Field(default=None, ge=0, le=500)
    usage_events_retention_hours: int | None = Field(default=None, ge=1, le=8760)
    default_filament_cost: float | None = None
    currency: str | None = None
    energy_cost_per_kwh: float | None = None
    spoolman_enabled: bool | None = None
    zigbee_enabled: bool | None = None
    device_labels_enabled: bool | None = None
    zigbee_transport: str | None = None
    zigbee_path: str | None = None
    spoolman_url: str | None = None
    spoolman_sync_mode: str | None = None
    spoolman_disable_weight_sync: bool | None = None
    spoolman_report_partial_usage: bool | None = None
    auto_add_unknown_rfid: bool | None = None
    disable_filament_warnings: bool | None = None
    spool_display_template: str | None = None
    check_updates: bool | None = None
    check_printer_firmware: bool | None = None
    include_beta_updates: bool | None = None
    local_login_enabled: bool | None = None  # #1589
    telemetry_enabled: bool | None = None
    language: str | None = None
    bed_cooled_threshold: float | None = None
    ams_humidity_good: int | None = None
    ams_humidity_fair: int | None = None
    ams_temp_good: float | None = None
    ams_temp_fair: float | None = None
    ams_history_retention_days: int | None = None
    plug_power_history_retention_days: int | None = Field(default=None, ge=1, le=365)
    sensor_history_retention_days: int | None = Field(default=None, ge=1, le=365)
    plug_power_sample_seconds: int | None = Field(default=None, ge=10, le=3600)
    printer_sensor_history_retention_days: int | None = None
    prefer_lowest_filament: bool | None = None
    queue_shortest_first: bool | None = None
    preheat_enabled: bool | None = None
    preheat_filament_targets: str | None = None
    preheat_max_wait_seconds: int | None = Field(default=None, ge=60, le=3600)
    preheat_soak_seconds: int | None = Field(default=None, ge=0, le=1800)
    queue_drying_enabled: bool | None = None
    queue_drying_block: bool | None = None
    ambient_drying_enabled: bool | None = None
    print_drying_enabled: bool | None = None
    drying_presets: str | None = None
    ams_humidity_thresholds: str | None = None
    zigbee_sensor_reporting: str | None = None
    zigbee_sensor_stale_multiplier: str | None = None
    zigbee_sensor_poll_seconds: str | None = None
    stagger_enabled: bool | None = None
    stagger_concurrent: int | None = None
    stagger_interval_minutes: int | None = None
    stagger_wait_for_bed: bool | None = None
    per_printer_mapping_expanded: bool | None = None
    date_format: str | None = None
    time_format: str | None = None
    default_printer_id: int | None = None
    virtual_printer_enabled: bool | None = None
    virtual_printer_access_code: str | None = None
    virtual_printer_mode: str | None = None
    virtual_printer_archive_name_source: str | None = None
    dark_style: str | None = None
    dark_background: str | None = None
    dark_accent: str | None = None
    light_style: str | None = None
    light_background: str | None = None
    light_accent: str | None = None
    ftp_retry_enabled: bool | None = None
    ftp_retry_count: int | None = None
    ftp_retry_delay: int | None = None
    ftp_timeout: int | None = None
    telegram_registration_open: bool | None = None
    mqtt_enabled: bool | None = None
    mqtt_broker: str | None = None
    mqtt_port: int | None = None
    mqtt_username: str | None = None
    mqtt_password: str | None = None
    mqtt_topic_prefix: str | None = None
    mqtt_use_tls: bool | None = None
    external_url: str | None = None
    docker_compose_dir: str | None = None
    ha_enabled: bool | None = None
    ha_url: str | None = None
    ha_token: str | None = None
    library_disk_warning_gb: float | None = None
    library_all_files_recursive: bool | None = None
    firmware_batch_concurrency: int | None = None
    camera_view_mode: str | None = None
    slice_engine: str | None = None
    preferred_slicer: str | None = None
    open_in_slicer: str | None = None
    use_slicer_api: bool | None = None
    orcaslicer_api_url: str | None = None
    bambu_studio_api_url: str | None = None
    slicer_stall_timeout_minutes: int | None = Field(default=None, ge=1, le=240)
    prometheus_enabled: bool | None = None
    prometheus_token: str | None = None
    low_stock_threshold: float | None = Field(default=None, ge=0.1, le=99.9)
    session_max_hours: int | None = Field(default=None, ge=1, le=720)
    forecast_global_lead_time_days: int | None = Field(default=None, ge=0)
    user_notifications_enabled: bool | None = None
    ldap_enabled: bool | None = None
    ldap_server_url: str | None = None
    ldap_bind_dn: str | None = None
    ldap_bind_password: str | None = None
    ldap_search_base: str | None = None
    ldap_user_filter: str | None = None
    ldap_security: str | None = None
    ldap_group_mapping: str | None = None
    ldap_auto_provision: bool | None = None
    ldap_default_group: str | None = None
    local_backup_enabled: bool | None = None
    local_backup_schedule: str | None = None
    local_backup_time: str | None = None
    local_backup_retention: int | None = None
    local_backup_path: str | None = None
    obico_enabled: bool | None = None
    obico_ml_url: str | None = None
    obico_ml_token: str | None = None
    obico_sensitivity: str | None = None
    obico_action: str | None = None
    obico_poll_interval: int | None = Field(default=None, ge=5, le=120)
    obico_enabled_printers: str | None = None
    default_sidebar_order: str | None = None
    gcode_snippets: str | None = None

    @field_validator(*LAN_SERVICE_URL_SETTINGS)
    @classmethod
    def validate_lan_service_url(cls, v: str | None, info) -> str | None:
        """Validate outbound service URLs on save, not at request time.

        A bad value is then rejected with a message naming the field, instead of
        failing opaquely when the integration next runs. Every one of these
        services is commonly self-hosted on the same host or LAN as BamDude, so
        the LAN-service policy applies: loopback and RFC-1918 stay permitted,
        while cloud-metadata endpoints, numeric-encoded IPs, IPv4-mapped IPv6 and
        non-HTTP schemes are rejected.
        """
        if not v:
            return v  # empty means "not configured" — nothing to validate
        from backend.app.api.routes._url_safety import assert_safe_lan_service_url

        assert_safe_lan_service_url(v, label=info.field_name)
        return v

    @field_validator("docker_compose_dir")
    @classmethod
    def validate_docker_compose_dir(cls, v: str | None) -> str | None:
        """Keep the copy-and-paste update command free of shell injection.

        ⚠️ Validated on the WRITE path only. Doing it on the read schema as well
        would mean a single bad row — however it got there — 500s the entire
        settings GET and takes the app down with it, which is a worse outcome
        than rendering a string that has to be pasted into a shell by hand
        before it does anything at all.
        """
        if v is None or not v.strip():
            return v
        candidate = v.strip()
        if len(candidate) > _COMPOSE_DIR_MAX_LEN:
            raise ValueError(f"Compose directory must be at most {_COMPOSE_DIR_MAX_LEN} characters")
        if not _COMPOSE_DIR_ALLOWED.match(candidate):
            raise ValueError(
                "Compose directory may only contain path characters (letters, digits, space, and - _ . / \\ : ~)"
            )
        # ⚠️ A trailing backslash is the one survivor that would still break the
        # frontend's double-quoting: `cd "/opt/bam dude\"` escapes the closing
        # quote and swallows the rest of the line. Harmless — the shell just
        # waits for a terminator rather than running anything — but the operator
        # would be left staring at a continuation prompt, so refuse it here
        # rather than shipping a command that cannot work.
        if candidate.endswith("\\"):
            raise ValueError("Compose directory must not end with a backslash")
        return candidate

    @field_validator("gcode_snippets")
    @classmethod
    def validate_gcode_snippets(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            raise ValueError("gcode_snippets must be valid JSON or empty")
        if not isinstance(parsed, dict):
            raise ValueError("gcode_snippets must be a JSON object keyed by printer model")
        return v

    @field_validator("ldap_group_mapping")
    @classmethod
    def validate_ldap_group_mapping(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            raise ValueError("ldap_group_mapping must be valid JSON or empty")
        if not isinstance(parsed, dict):
            raise ValueError("ldap_group_mapping must be a JSON object mapping LDAP group DNs to BamDude group names")
        return v

    @field_validator("obico_enabled_printers")
    @classmethod
    def validate_obico_enabled_printers(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            raise ValueError("obico_enabled_printers must be valid JSON or empty")
        if not isinstance(parsed, list) or not all(isinstance(item, int) for item in parsed):
            raise ValueError("obico_enabled_printers must be a JSON array of printer IDs (integers)")
        return v

    @field_validator("obico_sensitivity")
    @classmethod
    def validate_obico_sensitivity(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in ("low", "medium", "high"):
            raise ValueError("obico_sensitivity must be 'low', 'medium', or 'high'")
        return v

    @field_validator("obico_action")
    @classmethod
    def validate_obico_action(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if v not in ("notify", "pause", "pause_and_off"):
            raise ValueError("obico_action must be 'notify', 'pause', or 'pause_and_off'")
        return v

    @field_validator("default_sidebar_order")
    @classmethod
    def validate_default_sidebar_order(cls, v: str | None) -> str | None:
        if v is None or v == "":
            return v
        try:
            parsed = json.loads(v)
        except json.JSONDecodeError:
            raise ValueError("default_sidebar_order must be valid JSON or empty")
        if isinstance(parsed, dict):
            order = parsed.get("order")
        elif isinstance(parsed, list):
            order = parsed
        else:
            raise ValueError("default_sidebar_order must be a JSON object with 'order' key or a JSON array")
        if not isinstance(order, list) or not all(isinstance(item, str) for item in order):
            raise ValueError("sidebar order must be an array of strings")
        return v
