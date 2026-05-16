import type { ArchivePlatesResponse, LibraryFilePlatesResponse } from '../types/plates';

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

const API_BASE = '/api/v1';

// Auth token storage
let authToken: string | null = localStorage.getItem('auth_token');

// Proactive-refresh plumbing — see ``scheduleProactiveRefresh`` below.
const REFRESH_SAFETY_MS = 60_000;
// setTimeout delays larger than int32 ms wrap negative ⇒ instant-fire.
// 2_147_483_000 ms ≈ 24.85 days.
const MAX_TIMEOUT_MS = 2_147_483_000;
let proactiveRefreshTimer: number | null = null;

/**
 * Decode a JWT payload (the middle base64url-encoded segment). We don't
 * verify the signature — that's the server's job — we only need ``exp``
 * to schedule a proactive refresh before the token dies.
 */
function decodeJwt(token: string): { exp?: number; iat?: number } | null {
  try {
    const payload = token.split('.')[1];
    if (!payload) return null;
    const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
    return JSON.parse(json);
  } catch {
    return null;
  }
}

function tokenExpiryMs(): number | null {
  if (!authToken) return null;
  const decoded = decodeJwt(authToken);
  if (!decoded?.exp) return null;
  return decoded.exp * 1000;
}

/**
 * True when the access token is within ``REFRESH_SAFETY_MS`` of expiry (or
 * already past it). ``request()`` checks this before sending so the network
 * call goes out with a fresh token instead of triggering the reactive 401
 * dance — which the browser's network panel logs even when JS recovers
 * cleanly. Returns ``false`` when we can't read the expiry (non-JWT token
 * or decode failure) — in that case the reactive path still catches it.
 */
function isTokenNearExpiry(): boolean {
  const exp = tokenExpiryMs();
  if (exp === null) return false;
  return Date.now() >= exp - REFRESH_SAFETY_MS;
}

/**
 * Arm a one-shot timer that fires ``refreshAccessToken()`` shortly before
 * the current token's ``exp``. Re-armed on every successful ``setAuthToken``
 * (login / refresh response / page reload with a still-valid stored token).
 *
 * Without this every long-idle tab would hit the reactive 401 path on
 * refocus — the request still completes (refresh + retry), but the browser
 * console fills with one red 401 entry per parallel query (10–40 of them on
 * Archives / Print queue). Proactive refresh keeps the token green so no
 * request leaves with a dead token in the first place.
 */
function scheduleProactiveRefresh() {
  if (proactiveRefreshTimer !== null) {
    window.clearTimeout(proactiveRefreshTimer);
    proactiveRefreshTimer = null;
  }
  const exp = tokenExpiryMs();
  if (exp === null) return;
  const delay = Math.max(0, exp - Date.now() - REFRESH_SAFETY_MS);
  proactiveRefreshTimer = window.setTimeout(() => {
    proactiveRefreshTimer = null;
    // Fire-and-forget: a successful refresh re-arms via setAuthToken; a
    // failed one falls through to the next request's reactive 401 handler
    // which dispatches ``bamdude:auth-invalidated``.
    refreshAccessToken();
  }, Math.min(delay, MAX_TIMEOUT_MS));
}

export function setAuthToken(token: string | null) {
  authToken = token;
  if (token) {
    localStorage.setItem('auth_token', token);
    scheduleProactiveRefresh();
  } else {
    localStorage.removeItem('auth_token');
    if (proactiveRefreshTimer !== null) {
      window.clearTimeout(proactiveRefreshTimer);
      proactiveRefreshTimer = null;
    }
  }
}

// Initial arm on module load — picks up a still-valid token left in
// localStorage from a previous session.
if (authToken) {
  scheduleProactiveRefresh();
}

export function getAuthToken(): string | null {
  return authToken;
}

// Stream token for image/video URLs loaded via <img>/<video> tags
// (these can't send Authorization headers, so a query param token is used)
let streamToken: string | null = null;

export function setStreamToken(token: string | null) {
  streamToken = token;
}

export function getStreamToken(): string | null {
  return streamToken;
}

/** Append the stream token to a URL if available (for <img>/<video> src). */
export function withStreamToken(url: string): string {
  if (!streamToken) return url;
  const sep = url.includes('?') ? '&' : '?';
  return `${url}${sep}token=${encodeURIComponent(streamToken)}`;
}

function parseContentDispositionFilename(header: string | null): string | null {
  if (!header) return null;
  // RFC 5987: filename*=utf-8''percent-encoded-name
  const rfc5987Match = header.match(/filename\*=(?:UTF-8|utf-8)''(.+?)(?:;|$)/);
  if (rfc5987Match) {
    try { return decodeURIComponent(rfc5987Match[1]); } catch { /* fall through */ }
  }
  // Standard: filename="name" or filename=name
  const standardMatch = header.match(/filename="?([^";\n]+)"?/);
  return standardMatch?.[1] || null;
}

// Sliding-session refresh plumbing. When the server rejects an access token
// with "Token has expired" / "Could not validate credentials", we try
// POST /auth/refresh once (the HttpOnly refresh cookie flows automatically
// because credentials: 'include' is set below). On success we replace the
// stored access token and retry the original request — transparent to the
// caller. Only when refresh ALSO fails do we dispatch the auth-invalidated
// event that redirects to /login.
//
// Concurrent 401s (typical on tab-refocus when 4–5 queries fire in parallel
// and the old access token is dead for all of them) coalesce onto a single
// /auth/refresh call via this module-level promise singleton — without it
// each query would spawn its own refresh, racing against rotation and
// tripping the backend's reuse-detection on the second one in.
const REFRESH_ERROR_MESSAGES = [
  'Could not validate credentials',
  'Token has expired',
];
// Messages that mean the user is really logged out (account deactivated,
// etc.) — these skip the refresh attempt and fall straight through to the
// invalidation event.
const NON_REFRESHABLE_401_MESSAGES = [
  'User not found or inactive',
  'Invalid API key',
  'API key has expired',
];
let refreshAccessTokenPromise: Promise<boolean> | null = null;

async function refreshAccessToken(): Promise<boolean> {
  if (refreshAccessTokenPromise) return refreshAccessTokenPromise;
  refreshAccessTokenPromise = (async () => {
    try {
      const response = await fetch(`${API_BASE}/auth/refresh`, {
        method: 'POST',
        cache: 'no-store',
        credentials: 'include',
      });
      if (!response.ok) return false;
      const body = await response.json().catch(() => null);
      if (body?.access_token) {
        setAuthToken(body.access_token);
        return true;
      }
      return false;
    } catch {
      return false;
    } finally {
      // Null-out only after the awaiting callers resolve so every queued
      // consumer sees the same outcome. Next 401 wave starts fresh.
      refreshAccessTokenPromise = null;
    }
  })();
  return refreshAccessTokenPromise;
}

/**
 * Turn a FastAPI error body's ``detail`` field into a human-readable string.
 *
 * Accepts the three shapes the backend emits:
 *   1. ``"Some message"`` — plain HTTPException detail. Returned verbatim.
 *   2. ``[{loc, msg, ...}, ...]`` — Pydantic 422 validation errors. Each
 *      entry's ``msg`` is surfaced (with the "Value error, " prefix
 *      stripped since it's noise from pydantic v2), multi-entry results
 *      are joined with newlines so toast / inline displays both render
 *      sensibly. Previous behaviour stringified the whole array as JSON,
 *      which leaked raw ``{"type":"value_error",...}`` to the user.
 *   3. ``{error, message, ...}`` / anything else — pull ``message`` then
 *      ``error``, fall back to JSON as the last resort so we don't lose
 *      debug info entirely when a backend endpoint returns a bespoke shape.
 */
function formatErrorDetail(detail: unknown, status: number): string {
  if (typeof detail === 'string') return detail;
  if (Array.isArray(detail)) {
    const messages = detail
      .map((entry) => {
        if (typeof entry === 'string') return entry;
        if (entry && typeof entry === 'object' && 'msg' in entry) {
          const raw = String((entry as { msg?: unknown }).msg ?? '');
          return raw.replace(/^Value error,\s*/, '');
        }
        return '';
      })
      .filter(Boolean);
    if (messages.length) return messages.join('\n');
  }
  if (detail && typeof detail === 'object') {
    const d = detail as Record<string, unknown>;
    if (typeof d.message === 'string') return d.message;
    if (typeof d.error === 'string') return d.error;
    try {
      return JSON.stringify(detail);
    } catch {
      // fall through
    }
  }
  return `HTTP ${status}`;
}

async function request<T>(
  endpoint: string,
  options: RequestInit = {},
  __isRetry = false,
): Promise<T> {
  // Pre-emptive refresh: if the access token is past or within
  // REFRESH_SAFETY_MS of expiry, await a refresh BEFORE issuing the request
  // so it goes out with a fresh token. Avoids the 401 burst that sprays the
  // browser network panel when 20+ parallel queries fire on tab-refocus
  // after a long idle. Coalesced across concurrent callers via
  // ``refreshAccessTokenPromise`` so one /auth/refresh covers them all.
  // Skipped on retry (the reactive path already handled it) and when the
  // proactive timer just fired but hadn't completed yet (the timer's own
  // call coalesces with this one).
  if (authToken && !__isRetry && isTokenNearExpiry()) {
    await refreshAccessToken();
  }

  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...options.headers as Record<string, string>,
  };

  // Add auth token if available
  if (authToken) {
    headers['Authorization'] = `Bearer ${authToken}`;
  }

  const response = await fetch(`${API_BASE}${endpoint}`, {
    ...options,
    cache: 'no-store', // Prevent browser caching of API responses
    // credentials: 'include' lets the refresh cookie flow on any auth-path
    // request the browser happens to make (logout, /auth/me, etc.) and is
    // the prerequisite for /auth/refresh to see the cookie when we retry.
    // Safe on other endpoints — there's no non-auth cookie for the app
    // that we'd care about leaking.
    credentials: 'include',
    headers,
  });

  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    const detail = error.detail;
    const message = formatErrorDetail(detail, response.status);

    if (response.status === 401) {
      const refreshable =
        !__isRetry &&
        REFRESH_ERROR_MESSAGES.some(m => message.includes(m));
      if (refreshable) {
        // Try to refresh once. Coalesced across concurrent 401s.
        const ok = await refreshAccessToken();
        if (ok) {
          return request<T>(endpoint, options, true);
        }
        // Refresh failed (no cookie, replay detected, refresh expired, etc.)
        // — fall through to the invalidated-session path below.
      }
      const terminal =
        REFRESH_ERROR_MESSAGES.some(m => message.includes(m)) ||
        NON_REFRESHABLE_401_MESSAGES.some(m => message.includes(m));
      if (terminal) {
        setAuthToken(null);
        // Broadcast so AuthContext can clear React state and redirect to
        // /login. Before sliding-session (§18.14) this was the first line
        // of defence; after sliding-session it's the last resort — refresh
        // was already attempted and failed.
        window.dispatchEvent(
          new CustomEvent('bamdude:auth-invalidated', { detail: { reason: 'token-expired', message } }),
        );
      }
    }

    throw new Error(message);
  }

  // Handle empty responses (204 No Content, etc.)
  const contentLength = response.headers.get('content-length');
  if (response.status === 204 || contentLength === '0') {
    return undefined as T;
  }

  return await response.json();
}

// Printer types
export interface Printer {
  id: number;
  name: string;
  serial_number: string;
  ip_address: string;
  access_code: string;
  model: string | null;
  location: string | null;  // Group/location name
  nozzle_count: number;  // 1 or 2, auto-detected from MQTT
  is_active: boolean;
  auto_archive: boolean;
  cleanup_after_print: boolean;
  mqtt_connection_timeout: number;
  external_camera_url: string | null;
  external_camera_type: string | null;  // "mjpeg", "rtsp", "snapshot"
  external_camera_enabled: boolean;
  external_camera_snapshot_url: string | null;  // optional single-frame override (#1177)
  camera_rotation: number;  // 0, 90, 180, 270 degrees
  plate_detection_enabled: boolean;  // Check plate before print
  plate_detection_roi?: PlateDetectionROI;  // ROI for plate detection
  stagger_interval_minutes: number;  // Per-printer stagger interval override (0 = system default)
  swap_mode_enabled: boolean;  // A1 Mini plate swapper
  swap_profile: string | null;  // Active swap-mode variant (see /macros/swap-profiles)
  require_plate_clear: boolean;  // Require plate-clear confirmation before next queued print
  created_at: string;
  updated_at: string;
}

export interface HMSError {
  code: string;
  attr: number;  // Attribute value for constructing wiki URL
  module: number;
  severity: number;  // 1=fatal, 2=serious, 3=common, 4=info
}

export interface AMSTray {
  id: number;
  tray_color: string | null;
  tray_type: string | null;
  tray_sub_brands: string | null;  // Full name like "PLA Basic", "PETG HF"
  tray_id_name: string | null;  // Bambu filament ID like "A00-Y2" (can decode to color)
  tray_info_idx: string | null;  // Filament preset ID like "GFA00" - maps to cloud setting_id
  remain: number;
  k: number | null;  // Pressure advance value (from tray or K-profile lookup)
  cali_idx: number | null;  // Calibration index for K-profile lookup
  tag_uid: string | null;  // RFID tag UID (any tag)
  tray_uuid: string | null;  // Bambu Lab spool UUID (32-char hex, only valid for Bambu Lab spools)
  nozzle_temp_min: number | null;  // Min nozzle temperature
  nozzle_temp_max: number | null;  // Max nozzle temperature
  drying_temp: number | null;      // RFID-recommended drying temp
  drying_time: number | null;      // RFID-recommended drying time (hours)
  state: number | null;            // AMS tray state: 9=empty, 10=spool present not loaded, 11=loaded
}

export interface AMSUnit {
  id: number;
  humidity: number | null;
  temp: number | null;
  is_ams_ht: boolean;  // True for AMS-HT (single spool), False for regular AMS (4 spools)
  tray: AMSTray[];
  serial_number: string;  // AMS unit serial number (from MQTT sn field)
  sw_ver: string;         // AMS firmware version (from get_version info.module ams/* entry)
  dry_time: number;       // Minutes remaining (0 = not drying, >0 = drying active)
  dry_status: number;     // 0=Off, 1=Checking, 2=Drying, 3=Cooling, 4=Stopping, 5=Error
  dry_sub_status: number; // 0=Off, 1=Heating, 2=Dehumidify
  dry_sf_reason: number[]; // Cannot-dry reasons (1=InsufficientPower, 8=NeedPluginPower)
  module_type: string;    // "ams", "n3f", "n3s"
}

export interface NozzleInfo {
  nozzle_type: string;  // canonical material: "stainless_steel" / "hardened_steel" / ...
  nozzle_flow: string;  // parsed flow: "standard" / "high_flow" / "tpu_high_flow"
  nozzle_diameter: string;  // e.g., "0.4"
}

export interface NozzleRackSlot {
  id: number;
  nozzle_type: string;
  nozzle_diameter: string;
  wear: number | null;
  stat: number | null;  // Nozzle status (e.g. mounted/docked)
  max_temp: number;
  serial_number: string;
  filament_color: string;  // RGBA hex ("00000000" = no filament)
  filament_id: string;
  filament_type: string;  // Material type (e.g. "PLA", "PETG")
}

export interface PrintOptions {
  // Core AI detectors
  spaghetti_detector: boolean;
  print_halt: boolean;
  halt_print_sensitivity: string;  // "low", "medium", "high" - spaghetti sensitivity
  first_layer_inspector: boolean;
  printing_monitor: boolean;
  buildplate_marker_detector: boolean;
  allow_skip_parts: boolean;
  // Additional AI detectors (decoded from cfg bitmask)
  nozzle_clumping_detector: boolean;
  nozzle_clumping_sensitivity: string;  // "low", "medium", "high"
  pileup_detector: boolean;
  pileup_sensitivity: string;  // "low", "medium", "high"
  airprint_detector: boolean;
  airprint_sensitivity: string;  // "low", "medium", "high"
  auto_recovery_step_loss: boolean;
  filament_tangle_detect: boolean;
}

export interface FilaSwitchState {
  installed: boolean;
  // in[track] = currently loaded slot for that track (-1 = empty)
  in_slots: number[];
  // out[track] = extruder this track terminates at (0 = right, 1 = left)
  out_extruders: number[];
  stat: number;
  info: number;
}

export interface PrinterStatus {
  id: number;
  name: string;
  connected: boolean;
  state: string | null;
  current_print: string | null;
  subtask_name: string | null;
  current_archive_id: number | null;
  current_plate_id: number | null;
  gcode_file: string | null;
  progress: number | null;
  remaining_time: number | null;
  layer_num: number | null;
  total_layers: number | null;
  temperatures: {
    bed?: number;
    bed_target?: number;
    bed_heating?: boolean;  // Actual heater state from MQTT
    nozzle?: number;
    nozzle_target?: number;
    nozzle_heating?: boolean;  // Actual heater state from MQTT
    nozzle_2?: number;  // Second nozzle for H2 series (dual nozzle)
    nozzle_2_target?: number;
    nozzle_2_heating?: boolean;  // Actual heater state from MQTT
    chamber?: number;
    chamber_target?: number;
    chamber_heating?: boolean;  // Actual heater state from MQTT
  } | null;
  cover_url: string | null;
  hms_errors: HMSError[];
  // Pause classification (RUNNING→PAUSE edge, see hms_errors.classify_pause_reason).
  // pause_reason: normalised key for routing — 'user' | 'filament_runout' |
  //   'door_open' | 'presence_check' | 'file_pause_command' |
  //   'ai_first_layer_defect' | 'ai_spaghetti' | 'foreign_object' |
  //   'plate_objects' | 'hms_other' | 'unknown'. Null when not paused or pre-edge.
  // pause_reason_label: operator-facing copy (precise HMS description or generic label).
  // pause_started_at: epoch seconds at which the pause began — drives the live
  //   "Paused N min" counter and survives F5.
  pause_reason: string | null;
  pause_reason_label: string | null;
  pause_started_at: number | null;
  ams: AMSUnit[];
  ams_exists: boolean;
  vt_tray: AMSTray[];  // Virtual tray / external spool(s)
  sdcard: boolean;  // SD card inserted
  store_to_sdcard: boolean;  // Store sent files on SD card
  timelapse: boolean;  // Timelapse recording active
  ipcam: boolean;  // Live view enabled
  wifi_signal: number | null;  // WiFi signal strength in dBm
  wired_network: boolean;  // Ethernet connection detected
  door_open: boolean;  // Enclosure door open (backend parses X1 family via home_flag, others via stat)
  nozzles: NozzleInfo[];  // Nozzle hardware info (index 0=left/primary, 1=right)
  nozzle_rack: NozzleRackSlot[];  // H2C 6-nozzle tool-changer rack
  print_options: PrintOptions | null;  // AI detection and print options
  // Calibration stage tracking
  stg_cur: number;  // Current stage number (-1 = not calibrating)
  stg_cur_name: string | null;  // Human-readable current stage name
  stg: number[];  // List of stage numbers in calibration sequence
  // Air conditioning mode (0=cooling, 1=heating)
  airduct_mode: number;
  // Print speed level (1=silent, 2=standard, 3=sport, 4=ludicrous)
  speed_level: number;
  // Chamber light on/off
  chamber_light: boolean;
  // Active extruder for dual nozzle (0=right, 1=left)
  active_extruder: number;
  // AMS mapping - which AMS is connected to which nozzle
  // Format: [ams_id_for_nozzle0, ams_id_for_nozzle1, ...] where -1 means no AMS
  ams_mapping: number[];
  // Per-AMS extruder mapping - extracted from each AMS unit's info field
  // Format: {ams_id: extruder_id} where extruder 0=right, 1=left
  // Note: JSON keys are always strings
  ams_extruder_map: Record<string, number>;
  // Filament Track Switch accessory — null when not installed. When present,
  // AMS slots aren't tied to a specific extruder; the FTS routes any slot to
  // either extruder, so per-extruder slot filtering must be skipped. Upstream
  // Bambuddy #1162.
  fila_switch: FilaSwitchState | null;
  // Currently loaded tray (global tray ID, 255 = no filament loaded, 254 = external spool)
  tray_now: number;
  // AMS status for filament change tracking (0=idle, 1=filament_change, 2=rfid_identifying, 3=assist, 4=calibration)
  ams_status_main: number;
  // AMS sub-status for filament change step (when main=1): 4=retraction, 6=load verification, 7=purge
  ams_status_sub: number;
  // mc_print_sub_stage - filament change step indicator used by OrcaSlicer/BambuStudio
  mc_print_sub_stage: number;
  // Timestamp of last AMS data update (for RFID refresh detection)
  last_ams_update: number;
  // Number of printable objects in current print (for skip objects feature)
  printable_objects_count: number;
  // Whether the active 3MF supports per-object skipping (slicer's
  // gcode_label_objects AND exclude_object both true in project settings).
  // Skip-objects button is gated on this AND printable_objects_count >= 2.
  skip_objects_supported: boolean;
  // Fan speeds (0-100 percentage, null if not available for this model)
  cooling_fan_speed: number | null;  // Part cooling fan
  big_fan1_speed: number | null;     // Auxiliary fan
  big_fan2_speed: number | null;     // Chamber/exhaust fan
  heatbreak_fan_speed: number | null; // Hotend heatbreak fan
  firmware_version: string | null;   // Firmware version from MQTT
  // Developer LAN mode: true = enabled, false = disabled, null = unknown
  developer_mode: boolean | null;
  // Currently executing macro name (null = no macro running)
  macro_executing: string | null;
  // Queue plate-clear gate (#961): true means the printer is waiting on user
  // confirmation before the next auto-dispatch; false means the gate is released.
  awaiting_plate_clear: boolean;
  // AMS drying support
  supports_drying: boolean;
}

export interface PrinterCreate {
  name: string;
  serial_number: string;
  ip_address: string;
  access_code: string;
  model?: string;
  location?: string;
  auto_archive?: boolean;
  cleanup_after_print?: boolean;
  mqtt_connection_timeout?: number;
  external_camera_url?: string | null;
  external_camera_type?: string | null;
  external_camera_enabled?: boolean;
  external_camera_snapshot_url?: string | null;  // optional single-frame override (#1177)
  camera_rotation?: number;
  plate_detection_enabled?: boolean;
  plate_detection_roi?: PlateDetectionROI;
  stagger_interval_minutes?: number;
  swap_mode_enabled?: boolean;
  swap_profile?: string | null;
  require_plate_clear?: boolean;
}

// Plate Detection
export interface PlateDetectionROI {
  x: number;  // X start % (0.0-1.0)
  y: number;  // Y start % (0.0-1.0)
  w: number;  // Width % (0.0-1.0)
  h: number;  // Height % (0.0-1.0)
}

export interface PlateDetectionResult {
  is_empty: boolean;
  confidence: number;
  difference_percent: number;
  message: string;
  has_debug_image: boolean;
  debug_image_url?: string;
  needs_calibration: boolean;
  light_warning?: boolean;
  reference_count?: number;
  max_references?: number;
  roi?: PlateDetectionROI;
}

export interface PlateDetectionStatus {
  available: boolean;
  calibrated: boolean;
  reference_count: number;
  max_references: number;
  message: string;
}

export interface CalibrationResult {
  success: boolean;
  message: string;
}

export interface PlateReference {
  index: number;
  label: string;
  timestamp: string;
  has_image: boolean;
  thumbnail_url: string;
}

// Archive types
export interface ArchiveDuplicate {
  id: number;
  print_name: string | null;
  created_at: string;
  match_type: 'exact' | 'similar';  // 'exact' = hash match, 'similar' = name match
}

export interface Archive {
  id: number;
  printer_id: number | null;
  project_id: number | null;
  project_name: string | null;
  filename: string;
  file_path: string;
  file_size: number;
  content_hash: string | null;
  // Hash of the UNPATCHED source (library file or prior archive) when known.
  // NULL for external prints. Dedup groups by `effective_hash`.
  source_content_hash: string | null;
  // Patch identifiers applied before upload (v1: informational).
  applied_patches: string[] | null;
  // Group key for duplicate detection: source_content_hash ?? content_hash.
  effective_hash: string | null;
  thumbnail_path: string | null;
  timelapse_path: string | null;
  source_3mf_path: string | null;
  f3d_path: string | null;
  duplicates: ArchiveDuplicate[] | null;
  duplicate_count: number;
  duplicate_sequence: number;  // 0 = original, 1+ = nth duplicate
  original_archive_id: number | null;  // ID of the first/original archive
  object_count: number | null;
  print_name: string | null;
  print_time_seconds: number | null;
  actual_time_seconds: number | null;  // Computed from started_at/completed_at
  time_accuracy: number | null;  // Percentage: 100 = perfect, >100 = faster than estimated
  filament_used_grams: number | null;
  filament_type: string | null;
  filament_color: string | null;
  layer_height: number | null;
  total_layers: number | null;
  nozzle_diameter: number | null;
  bed_temperature: number | null;
  bed_type: string | null;  // Build plate type from 3MF (e.g. "Cool Plate", "Textured PEI Plate")
  nozzle_temperature: number | null;
  sliced_for_model: string | null;  // Printer model this file was sliced for
  // Which plate of the source 3MF was actually printed (m038). NULL for
  // archives where the index couldn't be inferred (some legacy / external
  // prints) — frontend falls back to "first plate" behaviour.
  plate_index: number | null;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  extra_data: Record<string, unknown> | null;
  makerworld_url: string | null;
  designer: string | null;
  external_url: string | null;
  is_favorite: boolean;
  tags: string | null;
  notes: string | null;
  cost: number | null;
  photos: string[] | null;
  failure_reason: string | null;
  quantity: number;
  energy_kwh: number | null;
  energy_cost: number | null;
  swap_compatible: boolean;
  // Queue attribution (m019). Queue-driven archives carry the originating
  // queue_id + batch_id; external / direct-dispatch archives get queue_id
  // (the printer's default queue) but no batch_id.
  queue_id: number | null;
  batch_id: string | null;
  // Verbose diagnostic for failures — the "hover to see why" twin of
  // ``failure_reason`` (short cause code).
  error_message: string | null;
  created_at: string;
  // User tracking (Issue #206)
  created_by_id: number | null;
  created_by_username: string | null;
}

export interface ArchiveSlim {
  id: number;
  printer_id: number | null;
  print_name: string | null;
  filename: string;
  print_time_seconds: number | null;
  actual_time_seconds: number | null;
  filament_used_grams: number | null;
  filament_type: string | null;
  filament_color: string | null;
  status: string;
  started_at: string | null;
  completed_at: string | null;
  cost: number | null;
  quantity: number;
  created_at: string;
  thumbnail_path: string | null;
}

export interface PaginationMeta {
  total: number;
  current_page: number;
  per_page: number;
  last_page: number;
}

export interface PaginatedArchiveResponse {
  data: Archive[];
  meta: PaginationMeta;
}

export interface ArchiveFilterOptions {
  materials: string[];
  colors: string[];
  tags: string[];
}

export interface ArchiveListParams {
  page?: number;
  per_page?: number;
  all?: boolean;
  printer_id?: number;
  project_id?: number;
  date_from?: string;
  date_to?: string;
  search?: string;
  collection?: string;
  material?: string;
  colors?: string;
  color_mode?: string;
  favorites_only?: boolean;
  hide_failed?: boolean;
  hide_duplicates?: boolean;
  tag?: string;
  kind?: string;
  sort_by?: string;
}


export interface ArchiveStats {
  total_prints: number;
  successful_prints: number;
  failed_prints: number;
  total_print_time_hours: number;
  total_filament_grams: number;
  total_cost: number;
  prints_by_filament_type: Record<string, number>;
  prints_by_printer: Record<string, number>;
  average_time_accuracy: number | null;
  time_accuracy_by_printer: Record<string, number> | null;
  total_energy_kwh: number;
  total_energy_cost: number;
  // True when a date-filtered total-consumption query is running on incomplete
  // snapshot history (e.g. right after upgrade, before hourly snapshots have
  // a baseline). UI should explain why the number may undercount.
  energy_data_warming_up?: boolean;
}

export interface TagInfo {
  name: string;
  count: number;
}

export interface FailureAnalysis {
  period_days: number;
  total_prints: number;
  failed_prints: number;
  failure_rate: number;
  failures_by_reason: Record<string, number>;
  failures_by_filament: Record<string, number>;
  failures_by_printer: Record<string, number>;
  failures_by_hour: Record<number, number>;
  recent_failures: Array<{
    id: number;
    print_name: string;
    failure_reason: string | null;
    filament_type: string | null;
    printer_id: number | null;
    created_at: string | null;
  }>;
  trend: Array<{
    week_start: string;
    total_prints: number;
    failed_prints: number;
    failure_rate: number;
  }>;
}

// Archive Comparison types
export interface ComparisonArchiveInfo {
  id: number;
  print_name: string;
  status: string;
  created_at: string | null;
  printer_id: number | null;
  project_name: string | null;
}

export interface ComparisonField {
  field: string;
  label: string;
  unit: string | null;
  values: (string | number | null)[];
  raw_values: (string | number | null)[];
  has_difference: boolean;
}

export interface SuccessCorrelationInsight {
  field: string;
  label: string;
  insight: string;
  success_avg?: number;
  failed_avg?: number;
  success_values?: string[];
  failed_values?: string[];
}

export interface SuccessCorrelation {
  has_both_outcomes: boolean;
  message?: string;
  successful_count?: number;
  failed_count?: number;
  insights?: SuccessCorrelationInsight[];
}

export interface ArchiveComparison {
  archives: ComparisonArchiveInfo[];
  comparison: ComparisonField[];
  differences: ComparisonField[];
  success_correlation: SuccessCorrelation;
}

export interface SimilarArchive {
  archive: {
    id: number;
    print_name: string;
    status: string;
    created_at: string | null;
  };
  match_reason: string;
  match_score: number;
}

// Project types
export interface ProjectStats {
  total_archives: number;
  total_items: number;  // Sum of quantities (total items printed)
  completed_prints: number;  // Sum of quantities for completed prints (parts)
  failed_prints: number;
  queued_prints: number;
  in_progress_prints: number;
  total_print_time_hours: number;
  total_filament_grams: number;
  progress_percent: number | null;  // Plates progress (total_archives / target_count)
  parts_progress_percent: number | null;  // Parts progress (completed_prints / target_parts_count)
  estimated_cost: number;
  total_energy_kwh: number;
  total_energy_cost: number;
  remaining_prints: number | null;  // Remaining plates
  remaining_parts: number | null;  // Remaining parts
  bom_total_items: number;
  bom_completed_items: number;
  bom_cost: number;
}

export interface ProjectChildPreview {
  id: number;
  name: string;
  color: string | null;
  status: string;
  progress_percent: number | null;
}

export interface Project {
  id: number;
  name: string;
  description: string | null;
  color: string | null;
  status: string;  // active, completed, archived
  target_count: number | null;  // Target number of plates/print jobs
  target_parts_count: number | null;  // Target number of parts/objects
  notes: string | null;
  attachments: ProjectAttachment[] | null;
  tags: string | null;
  due_date: string | null;
  priority: string;  // low, normal, high, urgent
  budget: number | null;
  is_template: boolean;
  template_source_id: number | null;
  parent_id: number | null;
  parent_name: string | null;
  children: ProjectChildPreview[];
  // B.2 (#1155) — external link rendered as a clickable icon next to the
  // project name. Validated http(s) on the wire; null = no link.
  url: string | null;
  // B.2 (#1155) — filename of the cover photo inside the project's
  // attachments dir; serves as the card's hero image. Null = no cover.
  cover_image_filename: string | null;
  created_at: string;
  updated_at: string;
  stats?: ProjectStats;
}

export interface ProjectAttachment {
  filename: string;
  original_name: string;
  size: number;
  uploaded_at: string;
}

export interface ArchivePreview {
  id: number;
  print_name: string | null;
  thumbnail_path: string | null;
  status: string;
  filament_type: string | null;
  filament_color: string | null;
}

export interface ProjectListItem {
  id: number;
  name: string;
  description: string | null;
  color: string | null;
  status: string;
  target_count: number | null;  // Target number of plates/print jobs
  target_parts_count: number | null;  // Target number of parts/objects
  budget: number | null;
  created_at: string;
  archive_count: number;  // Number of print jobs (plates)
  total_items: number;  // Sum of quantities (total items printed, including failed)
  completed_count: number;  // Sum of quantities for completed prints only (parts)
  failed_count: number;  // Sum of quantities for failed prints
  queue_count: number;
  progress_percent: number | null;  // Plates progress
  archives: ArchivePreview[];
  url: string | null;
  cover_image_filename: string | null;
}

export interface ProjectCreate {
  name: string;
  description?: string;
  color?: string;
  target_count?: number;
  target_parts_count?: number;
  notes?: string;
  tags?: string;
  due_date?: string;
  priority?: string;
  budget?: number | null;
  parent_id?: number;
  url?: string | null;
}

export interface ProjectUpdate {
  name?: string;
  description?: string;
  color?: string;
  status?: string;
  target_count?: number;
  target_parts_count?: number;
  notes?: string;
  tags?: string;
  due_date?: string;
  priority?: string;
  budget?: number | null;
  parent_id?: number;
  url?: string | null;
}

// BOM Types - Tracks sourced/purchased parts (hardware, electronics, etc.)
export interface BOMItem {
  id: number;
  project_id: number;
  name: string;
  quantity_needed: number;
  quantity_acquired: number;
  unit_price: number | null;
  sourcing_url: string | null;
  archive_id: number | null;
  archive_name: string | null;
  stl_filename: string | null;
  remarks: string | null;
  sort_order: number;
  is_complete: boolean;
  created_at: string;
  updated_at: string;
}

export interface BOMItemCreate {
  name: string;
  quantity_needed?: number;
  unit_price?: number;
  sourcing_url?: string;
  archive_id?: number;
  stl_filename?: string;
  remarks?: string;
}

export interface BOMItemUpdate {
  name?: string;
  quantity_needed?: number;
  quantity_acquired?: number;
  unit_price?: number;
  sourcing_url?: string;
  archive_id?: number;
  stl_filename?: string;
  remarks?: string;
}

// Project Export/Import Types
export interface BOMItemExport {
  name: string;
  quantity_needed: number;
  quantity_acquired: number;
  unit_price: number | null;
  sourcing_url: string | null;
  stl_filename: string | null;
  remarks: string | null;
}

export interface LinkedFolderExport {
  name: string;
}

export interface ProjectExport {
  name: string;
  description: string | null;
  color: string | null;
  status: string;
  target_count: number | null;
  target_parts_count: number | null;
  notes: string | null;
  tags: string | null;
  due_date: string | null;
  priority: string;
  budget: number | null;
  bom_items: BOMItemExport[];
  linked_folders: LinkedFolderExport[];
}

export interface ProjectImport {
  name: string;
  description?: string;
  color?: string;
  status?: string;
  target_count?: number;
  target_parts_count?: number;
  notes?: string;
  tags?: string;
  due_date?: string;
  priority?: string;
  budget?: number | null;
  bom_items?: BOMItemExport[];
  linked_folders?: LinkedFolderExport[];
}

// Print Plan Types
export interface PrintPlanItem {
  id: number;
  library_file_id: number;
  copies: number;
  order_index: number;
  filename: string;
  print_name: string | null;
  file_type: string;
  thumbnail_path: string | null;
  swap_compatible: boolean;
  filament_grams: number | null;
  print_time_seconds: number | null;
  object_count: number | null;
  cost_per_copy: number | null;
  total_filament_grams: number | null;
  total_print_time_seconds: number | null;
  total_objects: number | null;
  total_cost: number | null;
  // Per-(project, file) progress: count of completed archives + the
  // derived ``copies - printed_count`` remainder (clamped at 0).
  printed_count: number;
  remaining_count: number;
}

export interface PrintPlanResponse {
  items: PrintPlanItem[];
  totals_filament_grams: number;
  totals_print_time_seconds: number;
  totals_objects: number;
  totals_cost: number;
  default_filament_cost_per_kg: number;
}

// Timeline Types
export interface TimelineEvent {
  event_type: string;
  timestamp: string;
  title: string;
  description: string | null;
  metadata: Record<string, unknown> | null;
}

// API Key types
export interface APIKey {
  id: number;
  name: string;
  key_prefix: string;
  user_id: number | null;
  can_queue: boolean;
  can_control_printer: boolean;
  can_read_status: boolean;
  can_access_cloud: boolean;
  printer_ids: number[] | null;
  enabled: boolean;
  last_used: string | null;
  created_at: string;
  expires_at: string | null;
}

export interface APIKeyCreate {
  name: string;
  can_queue?: boolean;
  can_control_printer?: boolean;
  can_read_status?: boolean;
  can_access_cloud?: boolean;
  printer_ids?: number[] | null;
  expires_at?: string | null;
}

export interface APIKeyCreateResponse extends APIKey {
  key: string;  // Full key, only shown on creation
}

export interface APIKeyUpdate {
  name?: string;
  can_queue?: boolean;
  can_control_printer?: boolean;
  can_read_status?: boolean;
  can_access_cloud?: boolean;
  printer_ids?: number[] | null;
  enabled?: boolean;
  expires_at?: string | null;
}

// Long-lived camera-stream tokens (#1108)
export interface LongLivedToken {
  id: number;
  user_id: number;
  name: string;
  scope: string;
  lookup_prefix: string;
  created_at: string | null;
  expires_at: string | null;
  last_used_at: string | null;
  // Plaintext is only returned on create — null on every subsequent listing.
  token: string | null;
}

export interface LongLivedTokenCreate {
  name: string;
  expires_in_days: number;
  scope?: string;
}

// Settings types
export interface AppSettings {
  save_thumbnails: boolean;
  capture_finish_photo: boolean;
  archive_3mf_retention_enabled: boolean;
  archive_3mf_retention_days: number;
  default_filament_cost: number;
  currency: string;
  energy_cost_per_kwh: number;
  energy_tracking_mode: 'print' | 'total';
  check_updates: boolean;
  check_printer_firmware: boolean;
  include_beta_updates: boolean;
  language: string;
  // Telegram
  telegram_registration_open: boolean;
  // AMS threshold settings
  ams_humidity_good: number;  // <= this is green
  ams_humidity_fair: number;  // <= this is orange, > is red
  ams_temp_good: number;      // <= this is green/blue
  ams_temp_fair: number;      // <= this is orange, > is red
  ams_history_retention_days: number;  // days to keep AMS sensor history
  log_retention_days: number;  // days to keep historical bamdude-YYYY-MM-DD.log archives
  // Queue auto-drying settings
  queue_drying_enabled: boolean;  // Auto-dry AMS between queued prints
  queue_drying_block: boolean;  // Block queue until drying completes
  ambient_drying_enabled: boolean;  // Auto-dry idle printers based on humidity regardless of queue
  drying_presets: string;  // JSON blob of drying presets per filament type
  // Auto-queue routing
  queue_shortest_first: boolean;  // SJF + been_jumped guard for the auto-queue scheduler
  // Print modal settings
  per_printer_mapping_expanded: boolean;  // Whether custom mapping is expanded by default in print modal
  // Date/time format settings
  date_format: 'system' | 'us' | 'eu' | 'iso';
  time_format: 'system' | '12h' | '24h';
  // Filament tracking
  disable_filament_warnings: boolean;  // Disable filament warnings (print insufficiency and assignment mismatch)
  spool_display_template: string;  // Template for the synthesised spool display name (see utils/spoolName.ts)
  // Default printer
  default_printer_id: number | null;
  // Dark mode theme settings
  dark_style: 'classic' | 'glow' | 'vibrant';
  dark_background: 'neutral' | 'warm' | 'cool' | 'oled' | 'slate' | 'forest';
  dark_accent: 'green' | 'teal' | 'blue' | 'orange' | 'purple' | 'red';
  // Light mode theme settings
  light_style: 'classic' | 'glow' | 'vibrant';
  light_background: 'neutral' | 'warm' | 'cool';
  light_accent: 'green' | 'teal' | 'blue' | 'orange' | 'purple' | 'red';
  // FTP retry settings
  ftp_retry_enabled: boolean;
  ftp_retry_count: number;
  ftp_retry_delay: number;
  ftp_timeout: number;
  // MQTT relay settings
  mqtt_enabled: boolean;
  mqtt_broker: string;
  mqtt_port: number;
  mqtt_username: string;
  mqtt_password: string;
  mqtt_topic_prefix: string;
  mqtt_use_tls: boolean;
  // External URL for notifications
  external_url: string;
  // Home Assistant integration
  ha_enabled: boolean;
  ha_url: string;
  ha_token: string;
  ha_url_from_env: boolean;
  ha_token_from_env: boolean;
  ha_env_managed: boolean;
  // File Manager / Library settings
  library_disk_warning_gb: number;
  // Camera view settings
  camera_view_mode: 'window' | 'embedded';
  // Preferred slicer
  preferred_slicer: 'bambu_studio' | 'orcaslicer';
  // Server-side slicing (B.4): when true, the SliceModal entry points
  // appear in the file manager and archive context menus and the backend
  // dispatches via the configured sidecar URL below.
  use_slicer_api?: boolean;
  // Sidecar URLs for the OrcaSlicer / BambuStudio HTTP API. Empty = fall
  // back to env defaults configured on the server.
  orcaslicer_api_url?: string;
  bambu_studio_api_url?: string;
  // Per-model auto-print G-code snippets (#422). JSON object keyed by printer
  // model name → { start_gcode, end_gcode }. Empty string = none configured.
  gcode_snippets?: string;
  // Prometheus metrics
  prometheus_enabled: boolean;
  prometheus_token: string;
  // Bed cooled threshold
  bed_cooled_threshold: number;
  // Inventory low stock threshold
  low_stock_threshold: number;
  // Stock forecasting (upstream #1184): global floor applied on top of each SKU's lead time
  forecast_global_lead_time_days: number;
  // User email notifications toggle
  user_notifications_enabled: boolean;
  // Default sidebar order (admin-set for all users)
  default_sidebar_order: string;
  // Staggered start settings (electrical load management for farms)
  stagger_enabled: boolean;
  stagger_concurrent: number;
  stagger_interval_minutes: number;
  stagger_wait_for_bed: boolean;
  stagger_strict_for_direct_dispatch: boolean;
  // LDAP authentication
  ldap_enabled: boolean;
  ldap_server_url: string;
  ldap_bind_dn: string;
  ldap_bind_password: string;
  ldap_search_base: string;
  ldap_user_filter: string;
  ldap_security: string;
  ldap_group_mapping: string;
  ldap_auto_provision: boolean;
  ldap_default_group: string;
  // Scheduled local backup (#884)
  local_backup_enabled: boolean;
  local_backup_schedule: string;
  local_backup_time: string;
  local_backup_retention: number;
  local_backup_path: string;
  // Obico AI failure detection (#172)
  obico_enabled: boolean;
  obico_ml_url: string;
  obico_sensitivity: 'low' | 'medium' | 'high';
  obico_action: 'notify' | 'pause' | 'pause_and_off';
  obico_poll_interval: number;
  obico_enabled_printers: string;
}

export type AppSettingsUpdate = Partial<AppSettings>;

// Obico AI failure detection (#172)
export interface ObicoDetectionEvent {
  printer_id: number;
  task_name: string;
  timestamp: string;
  current_p: number;
  score: number;
  class: 'safe' | 'warning' | 'failure';
  detections: number;
}

export interface ObicoStatus {
  is_running: boolean;
  last_error: string | null;
  per_printer: Record<string, { class: string; frame_count: number; score: number }>;
  thresholds: { low: number; high: number };
  history: ObicoDetectionEvent[];
  enabled: boolean;
  ml_url: string;
  sensitivity: 'low' | 'medium' | 'high';
  action: 'notify' | 'pause' | 'pause_and_off';
  poll_interval: number;
  external_url_configured: boolean;
}

export interface ObicoTestConnection {
  ok: boolean;
  status_code: number | null;
  body: string | null;
  error: string | null;
}

// MQTT relay status
export interface MQTTStatus {
  enabled: boolean;
  connected: boolean;
  broker: string;
  port: number;
  topic_prefix: string;
}

// Cloud types
export interface CloudAuthStatus {
  is_authenticated: boolean;
  email: string | null;
  region?: 'global' | 'china' | null;
}

export interface CloudLoginResponse {
  success: boolean;
  needs_verification: boolean;
  message: string;
  verification_type?: 'email' | 'totp' | null;
  tfa_key?: string | null;
}

export interface SlicerSetting {
  setting_id: string;
  name: string;
  type: string;
  version: string | null;
  user_id: string | null;
  updated_time: string | null;
  is_custom: boolean;
}

export interface SpoolCatalogEntry {
  id: number;
  name: string;
  weight: number;
  is_default: boolean;
}

export interface ColorCatalogEntry {
  id: number;
  manufacturer: string;
  color_name: string;
  hex_color: string;
  material: string | null;
  is_default: boolean;
}

export interface ColorLookupResult {
  found: boolean;
  hex_color: string | null;
  material: string | null;
}

export interface SlicerSettingsResponse {
  filament: SlicerSetting[];
  printer: SlicerSetting[];
  process: SlicerSetting[];
}

export interface SlicerSettingDetail {
  message?: string | null;
  code?: string | null;
  error?: string | null;
  public: boolean;
  version?: string | null;
  type: string;
  name: string;
  update_time?: string | null;
  nickname?: string | null;
  base_id?: string | null;
  setting: Record<string, unknown>;
  filament_id?: string | null;
  setting_id?: string | null;
}

export interface SlicerSettingCreate {
  type: string;  // 'filament', 'print', or 'printer'
  name: string;
  base_id: string;
  setting: Record<string, unknown>;
}

export interface SlicerSettingUpdate {
  name?: string;
  setting?: Record<string, unknown>;
}

export interface SlicerSettingDeleteResponse {
  success: boolean;
  message: string;
}

// Built-in filament fallback (static table from backend)
export interface BuiltinFilament {
  filament_id: string;
  name: string;
}

// MakerWorld URL-paste import flow (B.5 — 0.5.x cycle).
export interface MakerworldStatus {
  has_cloud_token: boolean;
  can_download: boolean;
}

export interface MakerworldAlreadyImportedEntry {
  library_file_id: number;
  folder_id: number | null;
  filename: string;
}

export interface MakerworldResolvedModel {
  model_id: number;
  profile_id: number | null;
  design: Record<string, unknown>;
  instances: Array<Record<string, unknown>>;
  already_imported_library_ids: number[];
  // Per-variant dedupe map: profileId (stringified) → existing library row info.
  // Key "0" is reserved for legacy whole-model imports (no #profileId fragment).
  already_imported_by_profile_id: Record<string, MakerworldAlreadyImportedEntry>;
}

export interface MakerworldImportResponse {
  library_file_id: number;
  filename: string;
  folder_id: number | null;
  profile_id: number | null;
  was_existing: boolean;
}

export interface MakerworldRecentImport {
  library_file_id: number;
  filename: string;
  folder_id: number | null;
  thumbnail_path: string | null;
  source_url: string | null;
  created_at: string;
  title?: string | null;
  author_name?: string | null;
  sliced_for?: string | null;
  profile_id?: number | null;
  has_cover?: boolean;
  has_variant_cover?: boolean;
}

export interface MakerworldImportsPage {
  data: MakerworldRecentImport[];
  meta: {
    total: number;
    current_page: number;
    per_page: number;
    last_page: number;
  };
}

export type MakerworldImportsSortBy = 'date-desc' | 'date-asc' | 'name-asc' | 'name-desc';

export interface MakerworldImportsListParams {
  page?: number;
  per_page?: number;
  search?: string;
  sort_by?: MakerworldImportsSortBy;
}

// Local preset types (OrcaSlicer imports)
export interface LocalPreset {
  id: number;
  name: string;
  preset_type: string;
  source: string;
  filament_type: string | null;
  filament_vendor: string | null;
  nozzle_temp_min: number | null;
  nozzle_temp_max: number | null;
  pressure_advance: string | null;
  default_filament_colour: string | null;
  filament_cost: string | null;
  filament_density: string | null;
  compatible_printers: string | null;
  inherits: string | null;
  version: string | null;
  created_at: string;
  updated_at: string;
}

export interface LocalPresetDetail extends LocalPreset {
  setting: Record<string, unknown>;
}

export interface LocalPresetsResponse {
  filament: LocalPreset[];
  printer: LocalPreset[];
  process: LocalPreset[];
}

// =====================================================================
// Server-side slicing (B.4 — Phase 2 of 0.5.x cycle)
// =====================================================================

export type PresetSource = 'cloud' | 'local' | 'standard';

export interface PresetRef {
  source: PresetSource;
  id: string;
}

export type BedType =
  | 'Cool Plate'
  | 'Engineering Plate'
  | 'High Temp Plate'
  | 'Textured PEI Plate'
  | 'Supertack Plate';

export interface SliceBundleSpec {
  bundle_id: string;
  printer_name: string;
  process_name: string;
  filament_names: string[];
}

export interface SlicerBundle {
  id: string;
  printer_preset_name: string;
  printer: string[];
  process: string[];
  filament: string[];
  version: string | null;
}

export interface SliceRequest {
  printer_preset_id?: number;
  process_preset_id?: number;
  filament_preset_id?: number;
  printer_preset?: PresetRef;
  process_preset?: PresetRef;
  filament_preset?: PresetRef;
  // Multi-color: one PresetRef per plate slot, in plate order. Always
  // preferred over the singular `filament_preset` when both are sent; the
  // backend validator promotes a singular into a one-element list when this
  // is omitted, so legacy single-color clients keep working unchanged.
  filament_presets?: PresetRef[];
  plate?: number;
  export_3mf?: boolean;
  // Per-job slicer override. When the user has both OrcaSlicer and
  // BambuStudio sidecars configured, the SliceModal exposes a radio so the
  // slicer can be picked per source file. Falls back to the global
  // preferred_slicer setting when omitted.
  slicer?: 'orcaslicer' | 'bambu_studio';
  // Bed plate override — forwarded to the sidecar as ``bedType``, becomes
  // ``--curr-bed-type`` on the slicer CLI. Without this the CLI falls back
  // to the source 3MF's per-plate bed_type (when present) and finally to
  // "Cool Plate" (the upstream default — wrong for X1/A1 users who actually
  // use Textured PEI / SuperTack).
  bed_type?: BedType;
  // Optional Printer Preset Bundle reference. When set, the dispatcher
  // skips PresetRef resolution and asks the sidecar to materialise the
  // printer / process / filament JSONs from the stored bundle by name.
  // Mutually exclusive with the *_preset / *_preset_id fields.
  bundle?: SliceBundleSpec;
}

export interface SlicerHealth {
  healthy: boolean;
  url: string | null;
  version?: string;
  error?: string;
}

// GET /api/v1/slicer/presets — unified listing across cloud / local / standard.
export type SlicerCloudStatus = 'ok' | 'not_authenticated' | 'expired' | 'unreachable';

export interface UnifiedPreset {
  id: string;
  name: string;
  source: PresetSource;
  // Populated for the filament slot only — used by the SliceModal multi-color
  // pre-pick to score presets against each plate slot's required (type,
  // colour). Optional because the bundled / standard tier rarely carries a
  // colour (colour is a runtime spool attribute on Bambu).
  filament_type?: string | null;
  filament_colour?: string | null;
}

export interface UnifiedPresetsBySlot {
  printer: UnifiedPreset[];
  process: UnifiedPreset[];
  filament: UnifiedPreset[];
}

export interface UnifiedPresetsResponse {
  cloud: UnifiedPresetsBySlot;
  local: UnifiedPresetsBySlot;
  standard: UnifiedPresetsBySlot;
  cloud_status: SlicerCloudStatus;
}

export interface SliceResponse {
  library_file_id: number;
  name: string;
  print_time_seconds: number;
  filament_used_g: number;
  filament_used_mm: number;
  used_embedded_settings: boolean;
}

export interface SliceArchiveResponse {
  archive_id: number;
  name: string;
  print_time_seconds: number;
  filament_used_g: number;
  filament_used_mm: number;
  used_embedded_settings: boolean;
}

// Background slice-job lifecycle. POST /slice returns 202 + this shape;
// the frontend polls /slice-jobs/{id} until status is terminal.
export type SliceJobStatus = 'pending' | 'running' | 'completed' | 'failed';

export interface SliceJobEnqueueResponse {
  job_id: number;
  status: SliceJobStatus;
  status_url: string;
}

export interface SliceJobProgress {
  /** Stage label emitted by the slicer ("Generating G-code", "Slicing finished"). */
  stage: string;
  total_percent: number;
  plate_percent: number;
  /** 1-indexed plate position; 0 means "all plates" / final completion. */
  plate_index: number;
  plate_count: number;
  updated_at: number;
}

export interface SliceJobState {
  job_id: number;
  status: SliceJobStatus;
  kind: 'library_file' | 'archive';
  source_id: number;
  source_name: string;
  created_at: string;
  started_at: string | null;
  completed_at: string | null;
  /** Live progress fed by the sidecar's --pipe channel; null until the
   *  slicer emits its first frame (early "Initializing" phase) or when the
   *  sidecar doesn't support progress. */
  progress: SliceJobProgress | null;
  result?: SliceResponse | SliceArchiveResponse;
  error_status?: number;
  error_detail?: string;
}

export interface ImportResponse {
  success: boolean;
  imported: number;
  skipped: number;
  errors: string[];
}

export interface FieldOption {
  value: string;
  label: string;
}

export interface FieldDefinition {
  key: string;
  label: string;
  type: 'text' | 'number' | 'boolean' | 'select';
  category: string;
  description?: string;
  options?: FieldOption[];
  unit?: string;
  min?: number;
  max?: number;
  step?: number;
}

export interface FieldDefinitionsResponse {
  version: string;
  description: string;
  fields: FieldDefinition[];
}

export interface CloudDevice {
  dev_id: string;
  name: string;
  dev_model_name: string | null;
  dev_product_name: string | null;
  online: boolean;
}

// Smart Plug types
export interface SmartPlug {
  id: number;
  name: string;
  plug_type: 'tasmota' | 'homeassistant' | 'mqtt' | 'rest';
  ip_address: string | null;  // Required for Tasmota
  ha_entity_id: string | null;  // Required for Home Assistant (e.g., "switch.printer_plug", "script.turn_on_printer")
  // Home Assistant energy sensor entities (optional)
  ha_power_entity: string | null;
  ha_energy_today_entity: string | null;
  ha_energy_total_entity: string | null;
  // MQTT fields (required when plug_type="mqtt")
  // Legacy field - kept for backward compatibility
  mqtt_topic: string | null;  // Deprecated, use mqtt_power_topic
  mqtt_multiplier: number;  // Deprecated, use mqtt_power_multiplier
  // Power monitoring
  mqtt_power_topic: string | null;  // Topic for power data
  mqtt_power_path: string | null;  // e.g., "power_l1" or "data.power"
  mqtt_power_multiplier: number;  // Unit conversion for power
  // Energy monitoring
  mqtt_energy_topic: string | null;  // Topic for energy data
  mqtt_energy_path: string | null;  // e.g., "energy_l1"
  mqtt_energy_multiplier: number;  // Unit conversion for energy
  // State monitoring
  mqtt_state_topic: string | null;  // Topic for state data
  mqtt_state_path: string | null;  // e.g., "state_l1" for ON/OFF
  mqtt_state_on_value: string | null;  // What value means "ON" (e.g., "ON", "true", "1")
  // REST/Webhook fields (required when plug_type="rest")
  rest_on_url: string | null;
  rest_on_body: string | null;
  rest_off_url: string | null;
  rest_off_body: string | null;
  rest_method: string | null;
  rest_headers: string | null;
  rest_status_url: string | null;
  rest_status_path: string | null;
  rest_status_on_value: string | null;
  rest_power_url: string | null;
  rest_power_path: string | null;
  rest_power_multiplier: number;
  rest_energy_url: string | null;
  rest_energy_path: string | null;
  rest_energy_multiplier: number;
  printer_id: number | null;
  enabled: boolean;
  auto_on: boolean;
  auto_off: boolean;
  auto_off_persistent: boolean;
  off_delay_mode: 'time' | 'temperature';
  off_delay_minutes: number;
  off_temp_threshold: number;
  username: string | null;
  password: string | null;
  // Power alerts
  power_alert_enabled: boolean;
  power_alert_high: number | null;
  power_alert_low: number | null;
  power_alert_last_triggered: string | null;
  // Schedule
  schedule_enabled: boolean;
  schedule_on_time: string | null;
  schedule_off_time: string | null;
  // Visibility options
  show_in_switchbar: boolean;
  show_on_printer_card: boolean;  // For scripts: show on printer card
  // Status
  last_state: string | null;
  last_checked: string | null;
  auto_off_executed: boolean;  // True when auto-off was triggered after print
  created_at: string;
  updated_at: string;
}

export interface SmartPlugCreate {
  name: string;
  plug_type?: 'tasmota' | 'homeassistant' | 'mqtt' | 'rest';
  ip_address?: string | null;  // Required for Tasmota
  ha_entity_id?: string | null;  // Required for Home Assistant
  // Home Assistant energy sensor entities (optional)
  ha_power_entity?: string | null;
  ha_energy_today_entity?: string | null;
  ha_energy_total_entity?: string | null;
  // MQTT fields (required when plug_type="mqtt")
  // Legacy fields - kept for backward compatibility
  mqtt_topic?: string | null;
  mqtt_multiplier?: number;
  // Power monitoring
  mqtt_power_topic?: string | null;
  mqtt_power_path?: string | null;
  mqtt_power_multiplier?: number;
  // Energy monitoring
  mqtt_energy_topic?: string | null;
  mqtt_energy_path?: string | null;
  mqtt_energy_multiplier?: number;
  // State monitoring
  mqtt_state_topic?: string | null;
  mqtt_state_path?: string | null;
  mqtt_state_on_value?: string | null;
  // REST fields
  rest_on_url?: string | null;
  rest_on_body?: string | null;
  rest_off_url?: string | null;
  rest_off_body?: string | null;
  rest_method?: string | null;
  rest_headers?: string | null;
  rest_status_url?: string | null;
  rest_status_path?: string | null;
  rest_status_on_value?: string | null;
  rest_power_url?: string | null;
  rest_power_path?: string | null;
  rest_power_multiplier?: number;
  rest_energy_url?: string | null;
  rest_energy_path?: string | null;
  rest_energy_multiplier?: number;
  printer_id?: number | null;
  enabled?: boolean;
  auto_on?: boolean;
  auto_off?: boolean;
  auto_off_persistent?: boolean;
  off_delay_mode?: 'time' | 'temperature';
  off_delay_minutes?: number;
  off_temp_threshold?: number;
  username?: string | null;
  password?: string | null;
  // Power alerts
  power_alert_enabled?: boolean;
  power_alert_high?: number | null;
  power_alert_low?: number | null;
  // Schedule
  schedule_enabled?: boolean;
  schedule_on_time?: string | null;
  schedule_off_time?: string | null;
  // Visibility options
  show_in_switchbar?: boolean;
  show_on_printer_card?: boolean;
}

export interface SmartPlugUpdate {
  name?: string;
  plug_type?: 'tasmota' | 'homeassistant' | 'mqtt' | 'rest';
  ip_address?: string | null;
  ha_entity_id?: string | null;
  // Home Assistant energy sensor entities (optional)
  ha_power_entity?: string | null;
  ha_energy_today_entity?: string | null;
  ha_energy_total_entity?: string | null;
  // MQTT fields (legacy)
  mqtt_topic?: string | null;
  mqtt_multiplier?: number;
  // MQTT power fields
  mqtt_power_topic?: string | null;
  mqtt_power_path?: string | null;
  mqtt_power_multiplier?: number;
  // MQTT energy fields
  mqtt_energy_topic?: string | null;
  mqtt_energy_path?: string | null;
  mqtt_energy_multiplier?: number;
  // MQTT state fields
  mqtt_state_topic?: string | null;
  mqtt_state_path?: string | null;
  mqtt_state_on_value?: string | null;
  // REST fields
  rest_on_url?: string | null;
  rest_on_body?: string | null;
  rest_off_url?: string | null;
  rest_off_body?: string | null;
  rest_method?: string | null;
  rest_headers?: string | null;
  rest_status_url?: string | null;
  rest_status_path?: string | null;
  rest_status_on_value?: string | null;
  rest_power_url?: string | null;
  rest_power_path?: string | null;
  rest_power_multiplier?: number;
  rest_energy_url?: string | null;
  rest_energy_path?: string | null;
  rest_energy_multiplier?: number;
  printer_id?: number | null;
  enabled?: boolean;
  auto_on?: boolean;
  auto_off?: boolean;
  auto_off_persistent?: boolean;
  off_delay_mode?: 'time' | 'temperature';
  off_delay_minutes?: number;
  off_temp_threshold?: number;
  username?: string | null;
  password?: string | null;
  // Power alerts
  power_alert_enabled?: boolean;
  power_alert_high?: number | null;
  power_alert_low?: number | null;
  // Schedule
  schedule_enabled?: boolean;
  schedule_on_time?: string | null;
  schedule_off_time?: string | null;
  // Visibility options
  show_in_switchbar?: boolean;
  show_on_printer_card?: boolean;
}

// Home Assistant entity for smart plug selection
export interface HAEntity {
  entity_id: string;
  friendly_name: string;
  state: string | null;
  domain: string;  // "switch", "light", "input_boolean", "script"
}

// Home Assistant sensor entity for energy monitoring
export interface HASensorEntity {
  entity_id: string;
  friendly_name: string;
  state: string | null;
  unit_of_measurement: string | null;  // "W", "kW", "kWh", "Wh"
}

export interface HATestConnectionResult {
  success: boolean;
  message: string | null;
  error: string | null;
}

export interface SmartPlugEnergy {
  power: number | null;  // Current watts
  voltage: number | null;  // Volts
  current: number | null;  // Amps
  today: number | null;  // kWh used today
  yesterday: number | null;  // kWh used yesterday
  total: number | null;  // Total kWh
  factor: number | null;  // Power factor (0-1)
  apparent_power: number | null;  // VA
  reactive_power: number | null;  // VAr
}

export interface SmartPlugStatus {
  state: string | null;
  reachable: boolean;
  device_name: string | null;
  energy: SmartPlugEnergy | null;
}

export interface SmartPlugTestResult {
  success: boolean;
  state: string | null;
  device_name: string | null;
}

// Tasmota Discovery types
export interface TasmotaScanStatus {
  running: boolean;
  scanned: number;
  total: number;
}

export interface DiscoveredTasmotaDevice {
  ip_address: string;
  name: string;
  module: number | null;
  state: string | null;
  discovered_at: string | null;
}

// Print Queue types
export interface PrintQueueItem {
  id: number;
  queue_id: number;
  printer_id?: number | null;  // Convenience - resolved from queue
  project_id?: number | null;
  waiting_reason: string | null;
  archive_id: number | null;
  library_file_id: number | null;
  position: number;
  scheduled_time: string | null;
  auto_off_after: boolean;
  manual_start: boolean;
  ams_mapping: number[] | null;
  plate_id: number | null;
  // Print options
  bed_levelling: boolean;
  flow_cali: boolean;
  layer_inspect: boolean;
  timelapse: boolean;
  use_ams: boolean;
  mesh_mode_fast_check: boolean;
  execute_swap_macros: boolean;
  swap_macro_events: string[] | null;
  /** Auto-Print G-code Injection (#422). When true, dispatch resolves the per-model
      gcode_snippets server setting and splices snippets into the plate gcode at
      `; MACHINE_START_GCODE_END` (start) and EOF (end) before FTP upload. */
  gcode_injection: boolean;
  status: 'pending' | 'printing' | 'completed' | 'failed' | 'skipped' | 'cancelled';
  started_at: string | null;
  completed_at: string | null;
  error_message: string | null;
  created_at: string;
  batch_id?: string | null;
  archive_name?: string | null;
  archive_thumbnail?: string | null;
  library_file_name?: string | null;
  library_file_thumbnail?: string | null;
  printer_name?: string | null;
  print_time_seconds?: number | null;
  filament_used_grams?: number | null;
  filament_type?: string | null;
  filament_color?: string | null;
  sliced_for_model?: string | null;
  created_by_id?: number | null;
  created_by_username?: string | null;
  // Virtual-item markers set by the backend for external / direct-dispatch
  // prints that have no DB row.  Real queue items leave these at
  // is_virtual=false / source=null.
  is_virtual?: boolean;
  source?: 'external' | 'bamdude_direct' | 'bamdude_queue' | null;
}

export interface StaggerSlotInfo {
  printer_id: number;
  printer_name: string;
  started_at: number;
  temp_reached_at: number | null;
  state: 'heating' | 'interval_wait';
  seconds_to_free: number;
  interval_seconds: number;
}

export interface StaggerState {
  enabled: boolean;
  concurrent: number;
  interval_minutes: number;
  wait_for_bed: boolean;
  slots: StaggerSlotInfo[];
  free_slots: number;
  next_free_in_seconds: number | null;
}

export interface PrintQueueItemCreate {
  queue_id: number;  // Required - which printer's queue
  archive_id?: number | null;
  library_file_id?: number | null;
  scheduled_time?: string | null;
  auto_off_after?: boolean;
  manual_start?: boolean;
  ams_mapping?: number[] | null;
  plate_id?: number | null;
  bed_levelling?: boolean;
  flow_cali?: boolean;
  layer_inspect?: boolean;
  timelapse?: boolean;
  use_ams?: boolean;
  mesh_mode_fast_check?: boolean;
  execute_swap_macros?: boolean;
  swap_macro_events?: string[] | null;
  gcode_injection?: boolean;
  quantity?: number;
  // Project to associate the resulting archive with
  project_id?: number;
}

export interface PrintQueueItemUpdate {
  queue_id?: number | null;  // Move to different queue
  position?: number;
  scheduled_time?: string | null;
  auto_off_after?: boolean;
  manual_start?: boolean;
  ams_mapping?: number[];
  plate_id?: number | null;
  bed_levelling?: boolean;
  flow_cali?: boolean;
  layer_inspect?: boolean;
  timelapse?: boolean;
  use_ams?: boolean;
  mesh_mode_fast_check?: boolean;
  execute_swap_macros?: boolean;
  swap_macro_events?: string[] | null;
  gcode_injection?: boolean;
}

export interface PrinterQueue {
  id: number;
  printer_id: number;
  printer_name?: string | null;
  printer_model?: string | null;
  printer_location?: string | null;
  status: 'idle' | 'printing' | 'paused' | 'error';
  is_paused: boolean;
  last_activity_at: string | null;
  current_item_id: number | null;
  pending_count: number;
  completed_count: number;
  failed_count: number;
  cancelled_count: number;
  skipped_count: number;
  total_count: number;
  created_at: string;
  updated_at: string;
}

export interface PrintQueueBulkUpdate {
  item_ids: number[];
  queue_id?: number | null;
  scheduled_time?: string | null;
  auto_off_after?: boolean;
  manual_start?: boolean;
  // Print options
  bed_levelling?: boolean;
  flow_cali?: boolean;
  layer_inspect?: boolean;
  timelapse?: boolean;
  use_ams?: boolean;
  mesh_mode_fast_check?: boolean;
  execute_swap_macros?: boolean;
  swap_macro_events?: string[] | null;
  gcode_injection?: boolean;
}

export interface PrintQueueBulkUpdateResponse {
  updated_count: number;
  skipped_count: number;
  message: string;
}

// Auto Queue types — see backend/app/schemas/auto_queue.py
export interface AutoQueueFilamentOverride {
  slot_id: number;
  type?: string | null;
  color?: string | null;
  force_color_match?: boolean;
}

export interface AutoQueueItem {
  id: number;
  archive_id: number | null;
  library_file_id: number | null;
  project_id: number | null;
  target_model: string | null;
  target_location: string | null;
  required_filament_types: string[] | null;
  filament_overrides: AutoQueueFilamentOverride[] | null;
  force_color_match: boolean;
  plate_id: number | null;
  position: number;
  scheduled_time: string | null;
  manual_start: boolean;
  auto_off_after: boolean;
  require_previous_success: boolean;
  bed_levelling: boolean;
  flow_cali: boolean;
  layer_inspect: boolean;
  timelapse: boolean;
  use_ams: boolean;
  mesh_mode_fast_check: boolean;
  execute_swap_macros: boolean;
  swap_macro_events: string[] | null;
  status: 'pending' | 'assigned' | 'cancelled';
  waiting_reason: string | null;
  assigned_to_item_id: number | null;
  assigned_at: string | null;
  cancelled_at: string | null;
  print_time_seconds: number | null;
  been_jumped: boolean;
  batch_id: string | null;
  created_at: string;
  created_by_id: number | null;
  archive_name?: string | null;
  archive_thumbnail?: string | null;
  library_file_name?: string | null;
  library_file_thumbnail?: string | null;
  created_by_username?: string | null;
  assigned_printer_id?: number | null;
  assigned_printer_name?: string | null;
}

export interface AutoQueueStats {
  completed_count: number;
  failed_count: number;
  cancelled_count: number;
  total_count: number;
}

export interface AutoQueueItemCreate {
  archive_id?: number | null;
  library_file_id?: number | null;
  project_id?: number | null;
  target_model?: string | null;
  target_location?: string | null;
  required_filament_types?: string[] | null;
  filament_overrides?: AutoQueueFilamentOverride[] | null;
  force_color_match?: boolean;
  plate_id?: number | null;
  plate_ids?: number[] | null;
  bed_levelling?: boolean;
  flow_cali?: boolean;
  layer_inspect?: boolean;
  timelapse?: boolean;
  use_ams?: boolean;
  mesh_mode_fast_check?: boolean;
  execute_swap_macros?: boolean;
  swap_macro_events?: string[] | null;
  scheduled_time?: string | null;
  manual_start?: boolean;
  auto_off_after?: boolean;
  require_previous_success?: boolean;
  quantity?: number;
}

export interface AutoQueueItemUpdate {
  position?: number | null;
  target_model?: string | null;
  target_location?: string | null;
  required_filament_types?: string[] | null;
  filament_overrides?: AutoQueueFilamentOverride[] | null;
  force_color_match?: boolean | null;
  scheduled_time?: string | null;
  manual_start?: boolean | null;
  auto_off_after?: boolean | null;
  require_previous_success?: boolean | null;
  bed_levelling?: boolean | null;
  flow_cali?: boolean | null;
  layer_inspect?: boolean | null;
  timelapse?: boolean | null;
  use_ams?: boolean | null;
  mesh_mode_fast_check?: boolean | null;
  execute_swap_macros?: boolean | null;
  swap_macro_events?: string[] | null;
}

// MQTT Logging types
export interface MQTTLogEntry {
  timestamp: string;
  topic: string;
  direction: 'in' | 'out';
  payload: Record<string, unknown>;
}

export interface MQTTLogsResponse {
  logging_enabled: boolean;
  logs: MQTTLogEntry[];
}

// K-Profile types
export interface KProfile {
  slot_id: number;
  extruder_id: number;
  nozzle_id: string;
  nozzle_diameter: string;
  filament_id: string;
  name: string;
  k_value: string;
  n_coef: string;
  ams_id: number;
  tray_id: number;
  setting_id: string | null;
}

export interface KProfileCreate {
  slot_id?: number;  // Storage slot, 0 for new profiles
  extruder_id?: number;
  nozzle_id: string;
  nozzle_diameter: string;
  filament_id: string;
  name: string;
  k_value: string;
  n_coef?: string;
  ams_id?: number;
  tray_id?: number;
  setting_id?: string | null;
}

export interface KProfileDelete {
  slot_id: number;  // cali_idx - calibration index to delete
  extruder_id: number;
  nozzle_id: string;  // e.g., "HH00-0.4"
  nozzle_diameter: string;  // e.g., "0.4"
  filament_id: string;  // Bambu filament identifier
  setting_id?: string | null;  // Setting ID (for X1C series)
}

export interface KProfilesResponse {
  profiles: KProfile[];
  nozzle_diameter: string;
  // Maps each live cali_idx → our stable filament_calibration.id so notes
  // (keyed by fc_id since m065) survive printer reorders.
  fc_id_by_cali_idx?: Record<number, number>;
}

export interface KProfileNote {
  // Stable identity since m065. `setting_id` is still accepted as a hint —
  // the backend resolves it to `filament_calibration_id` via the printer's
  // live K-profile list.
  filament_calibration_id?: number | null;
  setting_id?: string | null;
  note: string;
}

export interface KProfileNotesResponse {
  notes: Record<number, string>;  // filament_calibration_id -> note
}

// Slot Preset Mapping
export interface SlotPresetMapping {
  ams_id: number;
  tray_id: number;
  preset_id: string;
  preset_name: string;
}


// Notification Provider types
export type ProviderType = 'callmebot' | 'ntfy' | 'pushover' | 'telegram' | 'email' | 'discord' | 'webhook' | 'homeassistant';

export interface NotificationProvider {
  id: number;
  name: string;
  provider_type: ProviderType;
  enabled: boolean;
  config: Record<string, unknown>;
  // Print lifecycle events
  on_print_start: boolean;
  on_print_complete: boolean;
  on_print_failed: boolean;
  on_print_stopped: boolean;
  on_print_progress: boolean;
  on_print_missing_spool_assignment: boolean;
  on_print_paused: boolean;
  on_print_resumed: boolean;
  // Printer status events
  on_printer_offline: boolean;
  on_printer_error: boolean;
  on_filament_low: boolean;
  on_maintenance_due: boolean;
  // AMS environmental alarms (regular AMS)
  on_ams_humidity_high: boolean;
  on_ams_temperature_high: boolean;
  // AMS-HT environmental alarms
  on_ams_ht_humidity_high: boolean;
  on_ams_ht_temperature_high: boolean;
  // Build plate detection
  on_plate_not_empty: boolean;
  // Bed cooled
  on_bed_cooled: boolean;
  // First layer complete
  on_first_layer_complete: boolean;
  // Print queue events
  on_queue_job_added: boolean;
  on_queue_job_started: boolean;
  on_queue_job_waiting: boolean;
  on_queue_job_skipped: boolean;
  on_queue_job_failed: boolean;
  on_queue_completed: boolean;
  // Stock forecasting (scaffold, upstream #1184)
  on_stock_reorder_alert: boolean;
  on_stock_break_alert: boolean;
  // Quiet hours
  quiet_hours_enabled: boolean;
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  // Daily digest
  daily_digest_enabled: boolean;
  daily_digest_time: string | null;
  // Printer filter
  printer_id: number | null;
  // Status tracking
  last_success: string | null;
  last_error: string | null;
  last_error_at: string | null;
  // Timestamps
  created_at: string;
  updated_at: string;
}

export interface NotificationProviderCreate {
  name: string;
  provider_type: ProviderType;
  enabled?: boolean;
  config: Record<string, unknown>;
  // Print lifecycle events
  on_print_start?: boolean;
  on_print_complete?: boolean;
  on_print_failed?: boolean;
  on_print_stopped?: boolean;
  on_print_progress?: boolean;
  on_print_missing_spool_assignment?: boolean;
  on_print_paused?: boolean;
  on_print_resumed?: boolean;
  // Printer status events
  on_printer_offline?: boolean;
  on_printer_error?: boolean;
  on_filament_low?: boolean;
  on_maintenance_due?: boolean;
  // AMS environmental alarms (regular AMS)
  on_ams_humidity_high?: boolean;
  on_ams_temperature_high?: boolean;
  // AMS-HT environmental alarms
  on_ams_ht_humidity_high?: boolean;
  on_ams_ht_temperature_high?: boolean;
  // Build plate detection
  on_plate_not_empty?: boolean;
  // Bed cooled
  on_bed_cooled?: boolean;
  // First layer complete
  on_first_layer_complete?: boolean;
  // Print queue events
  on_queue_job_added?: boolean;

  on_queue_job_started?: boolean;
  on_queue_job_waiting?: boolean;
  on_queue_job_skipped?: boolean;
  on_queue_job_failed?: boolean;
  on_queue_completed?: boolean;
  // Stock forecasting (scaffold)
  on_stock_reorder_alert?: boolean;
  on_stock_break_alert?: boolean;
  // Quiet hours
  quiet_hours_enabled?: boolean;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
  // Daily digest
  daily_digest_enabled?: boolean;
  daily_digest_time?: string | null;
  // Printer filter
  printer_id?: number | null;
}

export interface NotificationProviderUpdate {
  name?: string;
  provider_type?: ProviderType;
  enabled?: boolean;
  config?: Record<string, unknown>;
  // Print lifecycle events
  on_print_start?: boolean;
  on_print_complete?: boolean;
  on_print_failed?: boolean;
  on_print_stopped?: boolean;
  on_print_progress?: boolean;
  on_print_missing_spool_assignment?: boolean;
  on_print_paused?: boolean;
  on_print_resumed?: boolean;
  // Printer status events
  on_printer_offline?: boolean;
  on_printer_error?: boolean;
  on_filament_low?: boolean;
  on_maintenance_due?: boolean;
  // AMS environmental alarms (regular AMS)
  on_ams_humidity_high?: boolean;
  on_ams_temperature_high?: boolean;
  // AMS-HT environmental alarms
  on_ams_ht_humidity_high?: boolean;
  on_ams_ht_temperature_high?: boolean;
  // Build plate detection
  on_plate_not_empty?: boolean;
  // Bed cooled
  on_bed_cooled?: boolean;
  // First layer complete
  on_first_layer_complete?: boolean;
  // Print queue events
  on_queue_job_added?: boolean;

  on_queue_job_started?: boolean;
  on_queue_job_waiting?: boolean;
  on_queue_job_skipped?: boolean;
  on_queue_job_failed?: boolean;
  on_queue_completed?: boolean;
  // Stock forecasting (scaffold)
  on_stock_reorder_alert?: boolean;
  on_stock_break_alert?: boolean;
  // Quiet hours
  quiet_hours_enabled?: boolean;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
  // Daily digest
  daily_digest_enabled?: boolean;
  daily_digest_time?: string | null;
  // Printer filter
  printer_id?: number | null;
}

// Git Backup types
export type ScheduleType = 'hourly' | 'daily' | 'weekly';

export interface GitBackupConfig {
  id: number;
  repository_url: string;
  has_token: boolean;
  branch: string;
  schedule_enabled: boolean;
  schedule_type: ScheduleType;
  backup_kprofiles: boolean;
  backup_cloud_profiles: boolean;
  backup_settings: boolean;
  backup_spools: boolean;
  backup_archives: boolean;
  enabled: boolean;
  provider: string;
  api_base_url: string | null;
  last_backup_at: string | null;
  last_backup_status: string | null;
  last_backup_message: string | null;
  last_backup_commit_sha: string | null;
  next_scheduled_run: string | null;
  created_at: string;
  updated_at: string;
}

export interface GitBackupConfigCreate {
  repository_url: string;
  access_token: string;
  branch?: string;
  schedule_enabled?: boolean;
  schedule_type?: ScheduleType;
  backup_kprofiles?: boolean;
  backup_cloud_profiles?: boolean;
  backup_settings?: boolean;
  backup_spools?: boolean;
  backup_archives?: boolean;
  enabled?: boolean;
  provider?: string;
  api_base_url?: string | null;
}

export interface GitBackupLog {
  id: number;
  config_id: number;
  started_at: string;
  completed_at: string | null;
  status: string;
  trigger: string;
  commit_sha: string | null;
  files_changed: number;
  error_message: string | null;
}

export interface GitBackupStatus {
  configured: boolean;
  enabled: boolean;
  is_running: boolean;
  progress: string | null;
  last_backup_at: string | null;
  last_backup_status: string | null;
  next_scheduled_run: string | null;
}

export interface GitTestConnectionResponse {
  success: boolean;
  message: string;
  repo_name: string | null;
  permissions: Record<string, boolean> | null;
}

export interface GitBackupTriggerResponse {
  success: boolean;
  message: string;
  log_id: number | null;
  commit_sha: string | null;
  files_changed: number;
}

// Scheduled local backup (#884)
export interface LocalBackupStatus {
  enabled: boolean;
  schedule: 'hourly' | 'daily' | 'weekly';
  time: string;            // "HH:MM"
  retention: number;
  path: string;            // empty string = use default_path
  default_path: string;
  is_running: boolean;
  last_backup_at: string | null;
  last_status: 'success' | 'failed' | null;
  last_message: string | null;
  next_run: string | null;
}

export interface LocalBackupFile {
  filename: string;
  size: number;            // bytes
  created_at: string;
}

export interface LocalBackupRunResponse {
  success: boolean;
  message: string;
  filename?: string;
}

export interface LocalBackupDeleteResponse {
  success: boolean;
  message: string;
}

export interface NotificationTestRequest {
  provider_type: ProviderType;
  config: Record<string, unknown>;
}

export interface NotificationTestResponse {
  success: boolean;
  message: string;
}

export interface BackgroundDispatchResponse {
  status: 'dispatched' | 'queued' | string;
  printer_id: number;
  archive_id?: number | null;
  filename: string;
  /** null when status='queued' (quantity > 1 route) — no direct dispatch happened. */
  dispatch_job_id: number | null;
  dispatch_position: number | null;
  batch_id?: string | null;
  queued_copies?: number;
}

// Provider-specific config types for reference
export interface CallMeBotConfig {
  phone: string;
  apikey: string;
}

export interface NtfyConfig {
  server?: string;
  topic: string;
  auth_token?: string | null;
}

export interface PushoverConfig {
  user_key: string;
  app_token: string;
  priority?: number;
}

export interface TelegramConfig {
  bot_token: string;
  chat_id: string;
}

export interface EmailConfig {
  smtp_server: string;
  smtp_port?: number;
  username: string;
  password: string;
  from_email: string;
  to_email: string;
  use_tls?: boolean;
}

// Notification Template types
export interface NotificationTemplate {
  id: number;
  event_type: string;
  name: string;
  title_template: string;
  body_template: string;
  is_default: boolean;
  created_at: string;
  updated_at: string;
}

export interface NotificationTemplateUpdate {
  title_template?: string;
  body_template?: string;
}

export interface EventVariablesResponse {
  event_type: string;
  event_name: string;
  variables: string[];
}

export interface TemplatePreviewRequest {
  event_type: string;
  title_template: string;
  body_template: string;
}

export interface TemplatePreviewResponse {
  title: string;
  body: string;
}

// Notification Log types
export interface NotificationLogEntry {
  id: number;
  provider_id: number;
  provider_name: string | null;
  provider_type: string | null;
  event_type: string;
  title: string;
  message: string;
  success: boolean;
  error_message: string | null;
  printer_id: number | null;
  printer_name: string | null;
  created_at: string;
}

export interface NotificationLogStats {
  total: number;
  success_count: number;
  failure_count: number;
  by_event_type: Record<string, number>;
  by_provider: Record<string, number>;
}

// Spoolman types
export interface SpoolmanStatus {
  enabled: boolean;
  connected: boolean;
  url: string | null;
}

export interface SkippedSpool {
  location: string;
  reason: string;
  filament_type: string | null;
  color: string | null;
}

export interface SpoolmanSyncResult {
  success: boolean;
  synced_count: number;
  skipped_count: number;
  skipped: SkippedSpool[];
  errors: string[];
}

export interface UnlinkedSpool {
  id: number;
  filament_name: string | null;
  filament_material: string | null;
  filament_vendor: string | null;
  filament_color_hex: string | null;
  remaining_weight: number | null;
  location: string | null;
}

export interface LinkedSpoolInfo {
  id: number;
  remaining_weight: number | null;
  filament_weight: number | null;
}

export interface LinkedSpoolsMap {
  linked: Record<string, LinkedSpoolInfo>; // tag (uppercase) -> spool info
}

export interface SpoolmanVendor {
  id: number;
  name: string;
}

export interface SpoolmanFilamentEntry {
  id: number;
  name: string;
  material: string | null;
  color_hex: string | null;
  color_name: string | null;
  weight: number | null;
  spool_weight: number | null;
  vendor: SpoolmanVendor | null;
}

export interface SpoolmanSlotAssignmentEnriched {
  printer_id: number;
  printer_name: string | null;
  ams_id: number;
  tray_id: number;
  spoolman_spool_id: number;
  ams_label: string | null;
}

export interface SpoolmanFilamentPatch {
  name?: string | null;
  spool_weight?: number | null;
  keep_existing_spools?: boolean;
}

export interface SpoolmanBulkCreateResult {
  created: InventorySpool[];
  failed: number;
  failed_count: number;
  requested_count: number;
  first_error?: string;
}

// Inventory types
export interface InventorySpool {
  id: number;
  material: string;
  subtype: string | null;
  color_name: string | null;
  rgba: string | null;
  brand: string | null;
  label_weight: number;
  core_weight: number;
  core_weight_catalog_id: number | null;
  weight_used: number;
  slicer_filament: string | null;
  slicer_filament_name: string | null;
  nozzle_temp_min: number | null;
  nozzle_temp_max: number | null;
  note: string | null;
  added_full: boolean | null;
  last_used: string | null;
  encode_time: string | null;
  tag_uid: string | null;
  tray_uuid: string | null;
  data_origin: string | null;
  tag_type: string | null;
  archived_at: string | null;
  created_at: string;
  updated_at: string;
  cost_per_kg: number | null;
  purchase_date: string | null;
  // "1.75" | "2.85" — NOT NULL on the DB side (defaulted in m020), but
  // typed as string here to keep the dropdown binding straightforward.
  filament_diameter: string;
  // Position inside a purchase bundle / batch. Solo spools stay NULL;
  // the quick-add auto-increment fills 1..N server-side.
  lot: number | null;
  last_scale_weight: number | null;
  last_weighed_at: string | null;
  // B.1 — multi-colour gradient stops + visual effect overlay.
  extra_colors: string | null;
  effect_type: string | null;
  // B.8 — per-spool category + low-stock threshold override (%, 1..99).
  category: string | null;
  low_stock_threshold_pct: number | null;
  storage_location?: string | null;
  k_profiles?: SpoolKProfile[];
}

// Spool label printing (B.1).
export type SpoolLabelTemplate = 'ams_30x15' | 'box_40x30' | 'box_62x29' | 'avery_5160' | 'avery_l7160';

export interface SpoolLabelEntry {
  id: number;
  /** Optional override for the label's bold central line. The frontend forwards
   *  the value composed by ``formatSpoolDisplayName`` against the user's
   *  ``settings.spool_display_template`` so the printed label matches the
   *  Inventory page. When omitted the backend composes a fallback as
   *  ``color_name → slicer_filament_name → "{brand} {material}"``. */
  display_name?: string | null;
}

export interface SpoolLabelRequest {
  spools: SpoolLabelEntry[];
  template: SpoolLabelTemplate;
}

export interface SpoolUsageRecord {
  id: number;
  spool_id: number;
  printer_id: number | null;
  print_name: string | null;
  weight_used: number;
  percent_used: number;
  status: string;
  cost: number | null;
  created_at: string;
}

export interface SpoolKProfile {
  id: number;
  spool_id: number;
  printer_id: number;
  extruder: number;
  nozzle_diameter: string;
  nozzle_type: string | null;
  k_value: number;
  name: string | null;
  cali_idx: number | null;
  setting_id: string | null;
  created_at: string;
}

export interface SpoolKProfileInput {
  printer_id: number;
  extruder?: number;
  nozzle_diameter?: string;
  nozzle_type?: string | null;
  k_value: number;
  name?: string | null;
  cali_idx?: number | null;
  setting_id?: string | null;
}

export interface SpoolAssignment {
  id: number;
  spool_id: number;
  printer_id: number;
  printer_name: string | null;
  ams_id: number;
  tray_id: number;
  fingerprint_color: string | null;
  fingerprint_type: string | null;
  spool?: InventorySpool | null;
  configured: boolean;
  pending_config?: boolean;  // Slot was empty at assign time; will configure on insert
  created_at: string;
  ams_label?: string | null;  // User-defined friendly name for the AMS unit
}

// Stock forecasting (upstream #1184) — per-SKU reorder configuration +
// shopping list. Algorithm runs entirely in ForecastPanel; these types
// describe the persistence layer.
export interface FilamentSkuSettings {
  id: number;
  material: string;
  subtype: string | null;
  brand: string | null;
  lead_time_days: number;
  safety_margin_value: number;
  safety_margin_unit: 'days' | 'g';
  alerts_snoozed: boolean;
}

export interface ShoppingListItem {
  id: number;
  material: string;
  subtype: string | null;
  brand: string | null;
  quantity_spools: number;
  note: string | null;
  status: 'pending' | 'purchased' | 'received';
  purchased_at: string | null;
  added_at: string;
}

export interface ShoppingListItemCreate {
  material: string;
  subtype: string | null;
  brand: string | null;
  quantity_spools: number;
  note?: string | null;
}

// Update types
export interface VersionInfo {
  version: string;
  repo: string;
}

export interface UpdateCheckResult {
  update_available: boolean;
  current_version: string;
  latest_version: string | null;
  is_prerelease?: boolean;
  release_name?: string;
  release_notes?: string;
  release_url?: string;
  published_at?: string;
  error?: string;
  message?: string;
  is_docker?: boolean;
  is_ha_addon?: boolean;
  update_method?: 'docker' | 'git' | 'ha_addon';
}

export interface UpdateStatus {
  status: 'idle' | 'checking' | 'downloading' | 'installing' | 'complete' | 'error';
  progress: number;
  message: string;
  error: string | null;
}

// Maintenance types
export interface MaintenanceType {
  id: number;
  name: string;
  type_code: string | null;
  description: string | null;
  default_interval_hours: number;
  interval_type: 'hours' | 'days';  // "hours" = print hours, "days" = calendar days
  icon: string | null;
  wiki_url: string | null;  // Documentation link
  printer_models: string[];  // ["*"] = all models, or specific model codes
  is_system: boolean;
  created_at: string;
}

export interface MaintenanceTypeCreate {
  name: string;
  description?: string | null;
  default_interval_hours?: number;
  interval_type?: 'hours' | 'days';
  icon?: string | null;
  wiki_url?: string | null;
  printer_models?: string[];
}

export interface MaintenanceStatus {
  id: number;
  printer_id: number;
  printer_name: string;
  printer_model: string | null;
  maintenance_type_id: number;
  maintenance_type_name: string;
  maintenance_type_code: string | null;
  maintenance_type_icon: string | null;
  maintenance_type_wiki_url: string | null;  // Custom wiki URL from type
  enabled: boolean;
  interval_hours: number;  // For hours type: print hours; for days type: number of days
  interval_type: 'hours' | 'days';
  current_hours: number;
  hours_since_maintenance: number;
  hours_until_due: number;
  days_since_maintenance: number | null;  // For days type
  days_until_due: number | null;  // For days type
  is_due: boolean;
  is_warning: boolean;
  last_performed_at: string | null;
}

export interface PrinterMaintenanceOverview {
  printer_id: number;
  printer_name: string;
  printer_model: string | null;
  printer_location: string | null;
  total_print_hours: number;
  maintenance_items: MaintenanceStatus[];
  due_count: number;
  warning_count: number;
}

export interface MaintenanceHistory {
  id: number;
  printer_maintenance_id: number;
  performed_at: string;
  hours_at_maintenance: number;
  notes: string | null;
}

export interface MaintenanceHistoryEntry {
  id: number;
  performed_at: string;
  hours_at_maintenance: number;
  notes: string | null;
  printer_name: string | null;
  maintenance_type_name: string | null;
  performed_by_user_id: number | null;
  performed_by_username: string | null;
  performed_by_chat_id: number | null;
  performed_by_chat_label: string | null;
}

export interface MaintenanceHistoryPage {
  items: MaintenanceHistoryEntry[];
  total: number;
  page: number;
  per_page: number;
  last_page: number;
}

export interface MaintenanceSummary {
  total_due: number;
  total_warning: number;
  printers_with_issues: Array<{
    printer_id: number;
    printer_name: string;
    due_count: number;
    warning_count: number;
  }>;
}

// External Links (sidebar)
export type ExternalLinkNavGroup = 'operations' | 'workshop' | 'resources' | 'care' | 'system' | 'external';

export interface ExternalLink {
  id: number;
  name: string;
  url: string;
  icon: string;
  open_in_new_tab: boolean;
  custom_icon: string | null;
  nav_group: ExternalLinkNavGroup;
  sort_order: number;
  created_at: string;
  updated_at: string;
}

export interface ExternalLinkCreate {
  name: string;
  url: string;
  icon: string;
  open_in_new_tab?: boolean;
  nav_group?: ExternalLinkNavGroup;
}

export interface ExternalLinkUpdate {
  name?: string;
  url?: string;
  icon?: string;
  open_in_new_tab?: boolean;
  nav_group?: ExternalLinkNavGroup;
}

// Permission type - all available permissions
export type Permission =
  | 'printers:read' | 'printers:create' | 'printers:update' | 'printers:delete' | 'printers:control' | 'printers:files' | 'printers:ams_rfid' | 'printers:clear_plate'
  | 'archives:read' | 'archives:create'
  | 'archives:update_own' | 'archives:update_all' | 'archives:delete_own' | 'archives:delete_all'
  | 'archives:reprint_own' | 'archives:reprint_all'
  | 'queue:read' | 'queue:create'
  | 'queue:update_own' | 'queue:update_all' | 'queue:delete_own' | 'queue:delete_all'
  | 'queue:reorder'
  | 'library:read' | 'library:upload'
  | 'library:update_own' | 'library:update_all' | 'library:delete_own' | 'library:delete_all'
  | 'library:purge' | 'archives:purge'
  | 'projects:read' | 'projects:create' | 'projects:update' | 'projects:delete'
  | 'filaments:read' | 'filaments:create' | 'filaments:update' | 'filaments:delete'
  | 'inventory:read' | 'inventory:create' | 'inventory:update' | 'inventory:delete' | 'inventory:view_assignments'
  | 'inventory:forecast_read' | 'inventory:forecast_write'
  | 'smart_plugs:read' | 'smart_plugs:create' | 'smart_plugs:update' | 'smart_plugs:delete' | 'smart_plugs:control'
  | 'camera:view'
  | 'maintenance:read' | 'maintenance:create' | 'maintenance:update' | 'maintenance:delete'
  | 'kprofiles:read' | 'kprofiles:create' | 'kprofiles:update' | 'kprofiles:delete'
  | 'notifications:read' | 'notifications:create' | 'notifications:update' | 'notifications:delete' | 'notifications:user_email'
  | 'notification_templates:read' | 'notification_templates:update'
  | 'external_links:read' | 'external_links:create' | 'external_links:update' | 'external_links:delete'
  | 'discovery:scan'
  | 'firmware:read' | 'firmware:update'
  | 'ams_history:read'
  | 'stats:read'
  | 'system:read'
  | 'settings:read' | 'settings:update' | 'settings:backup' | 'settings:restore'
  | 'git:backup' | 'git:restore'
  | 'cloud:auth'
  | 'makerworld:view' | 'makerworld:import'
  | 'api_keys:read' | 'api_keys:create' | 'api_keys:update' | 'api_keys:delete'
  | 'users:read' | 'users:create' | 'users:update' | 'users:delete'
  | 'groups:read' | 'groups:create' | 'groups:update' | 'groups:delete'
  | 'websocket:connect';

// Group types
export interface GroupBrief {
  id: number;
  name: string;
}

export interface Group {
  id: number;
  name: string;
  description: string | null;
  permissions: Permission[];
  is_system: boolean;
  user_count: number;
  created_at: string;
  updated_at: string;
}

export interface GroupDetail extends Group {
  users: Array<{ id: number; username: string; is_active: boolean }>;
}

export interface GroupCreate {
  name: string;
  description?: string;
  permissions: Permission[];
}

export interface GroupUpdate {
  name?: string;
  description?: string;
  permissions?: Permission[];
}

export interface PermissionInfo {
  value: Permission;
  label: string;
}

export interface PermissionCategory {
  name: string;
  permissions: PermissionInfo[];
}

export interface PermissionsListResponse {
  categories: PermissionCategory[];
  all_permissions: Permission[];
}

// User email notification preferences
export interface UserEmailPreferences {
  notify_print_start: boolean;
  notify_print_complete: boolean;
  notify_print_failed: boolean;
  notify_print_stopped: boolean;
}

// Per-(user, printer-model) saved PrintModal toggles. Lives on the
// `print_options_preferences` table. Read on modal open / model change,
// PUT on submit (direct print, queue add, auto-queue add).
export interface PrintOptionsPreferenceData {
  print_options: {
    bed_levelling: boolean;
    flow_cali: boolean;
    layer_inspect: boolean;
    timelapse: boolean;
    mesh_mode_fast_check: boolean;
    gcode_injection: boolean;
  };
  swap_macros: {
    execute: boolean;
    events: string[];
  };
}

export interface PrintOptionsPreferenceResponse {
  printer_model: string;
  options: PrintOptionsPreferenceData;
  updated_at: string;
}

// Admin list entry — preference + the user it belongs to. Returned by
// the cross-user list endpoint that powers the Settings → Print → Saved
// Profiles widget.
export interface PrintOptionsPreferenceAdminEntry {
  user_id: number;
  username: string;
  printer_model: string;
  options: PrintOptionsPreferenceData;
  updated_at: string;
}

export interface PrintOptionsPreferenceCopyRequest {
  src_user_id: number;
  src_printer_model: string;
  dst_user_id: number;
  // Defaults to src_printer_model server-side when omitted.
  dst_printer_model?: string;
}

// Auth types
export interface LoginRequest {
  username: string;
  password: string;
  // Sliding-session: when true the refresh token cookie gets Max-Age=30d and
  // the backing DB row lives that long; when false (default) the cookie is
  // session-scoped and the DB row caps at 12h (§18.14).
  remember_me?: boolean;
}

export interface LoginResponse {
  access_token?: string;
  token_type?: string;
  user?: UserResponse;
  /** Set when 2FA verification is required before a full token is issued. */
  requires_2fa?: boolean;
  pre_auth_token?: string;
  two_fa_methods?: string[];
}

// 2FA / MFA interfaces (§18.1)
export interface TwoFAStatus {
  totp_enabled: boolean;
  email_otp_enabled: boolean;
  backup_codes_remaining: number;
}

export interface TOTPSetupResponse {
  secret: string;
  qr_code_b64: string;
  issuer: string;
}

export interface TOTPEnableResponse {
  message: string;
  backup_codes: string[];
}

export interface BackupCodesResponse {
  backup_codes: string[];
  message: string;
}

export interface TwoFAVerifyRequest {
  pre_auth_token: string;
  code: string;
  method: 'totp' | 'email' | 'backup';
  // Propagated from the step-1 Remember-me checkbox so the sliding-session
  // refresh cookie issued on successful 2FA matches the user's choice
  // (§18.14). Default false.
  remember_me?: boolean;
}

// OIDC interfaces (§18.2)
export interface OIDCProvider {
  id: number;
  name: string;
  issuer_url: string;
  client_id: string;
  scopes: string;
  is_enabled: boolean;
  auto_create_users: boolean;
  auto_link_existing_accounts: boolean;
  /** JWT claim used as email identity. "email" (default) or e.g. "preferred_username"/"upn" for Azure Entra ID. */
  email_claim: string;
  /** Only consulted when email_claim === "email". Set false for legacy IdPs that never send email_verified. */
  require_email_verified: boolean;
  icon_url?: string | null;
  /** Operator-configurable default group for auto-created OIDC users (#1173).
   *  null → callback falls back to "Viewers". */
  default_group_id?: number | null;
}

export interface OIDCProviderCreate {
  name: string;
  issuer_url: string;
  client_id: string;
  client_secret: string;
  scopes?: string;
  is_enabled?: boolean;
  auto_create_users?: boolean;
  auto_link_existing_accounts?: boolean;
  email_claim?: string;
  require_email_verified?: boolean;
  icon_url?: string | null;
  default_group_id?: number | null;
}

export interface OIDCLink {
  id: number;
  provider_id: number;
  provider_name: string;
  provider_email?: string | null;
  created_at: string;
}

export interface UserResponse {
  id: number;
  username: string;
  email?: string;
  role: string;  // Deprecated, kept for backward compatibility
  is_active: boolean;
  is_admin: boolean;  // Computed from role and group membership
  auth_source: string;  // "local" or "ldap"
  groups: GroupBrief[];
  permissions: Permission[];  // All permissions from groups
  created_at: string;
}

export interface UserCreate {
  username: string;
  password?: string;  // Optional when advanced auth is enabled
  email?: string;
  role: string;
  group_ids?: number[];
}

export interface UserUpdate {
  username?: string;
  password?: string;
  email?: string;
  role?: string;
  is_active?: boolean;
  group_ids?: number[];
}

export interface SetupRequest {
  admin_username: string;
  admin_password: string;
  admin_email?: string;
}

export interface ForgotPasswordRequest {
  email: string;
}

export interface ForgotPasswordResponse {
  message: string;
}

export interface ResetPasswordRequest {
  user_id: number;
}

export interface ResetPasswordResponse {
  message: string;
}

export interface SMTPSettings {
  smtp_host: string;
  smtp_port: number;
  smtp_username?: string;
  smtp_password?: string;
  smtp_security: 'starttls' | 'ssl' | 'none';
  smtp_auth_enabled: boolean;
  smtp_from_email: string;
  smtp_from_name: string;
}

export interface TestSMTPRequest {
  test_recipient: string;
}

export interface TestSMTPResponse {
  success: boolean;
  message: string;
}

export interface AdvancedAuthStatus {
  advanced_auth_enabled: boolean;
  smtp_configured: boolean;
}

export interface LDAPStatus {
  ldap_enabled: boolean;
  ldap_configured: boolean;
}

export interface LDAPTestResponse {
  success: boolean;
  message: string;
}

export interface SetupResponse {
  admin_created: boolean;
  access_token: string;
  token_type: string;
  user: UserResponse;
}

export interface AuthStatus {
  /**
   * Legacy field. Kept for backward compatibility with older clients; the
   * opt-in auth mode has been removed and the backend always returns ``true``.
   */
  auth_enabled: boolean;
  requires_setup: boolean;
}

export interface EncryptionRowCounts {
  oidc_providers: number;
  user_totp: number;
}

export interface EncryptionStatus {
  key_configured: boolean;
  key_source: 'env' | 'file' | 'generated' | 'none';
  legacy_plaintext_rows: EncryptionRowCounts;
  encrypted_rows: EncryptionRowCounts;
  decryption_broken: boolean;
  migration_error_count: number;
}

// AMS Settings dialog (BS port). Mirrors backend/app/schemas/ams_settings.py.
export interface AmsSystemSettingState {
  insertion_update: boolean | null;
  power_on_update: boolean | null;
  remain_capacity: boolean | null;
  auto_switch_filament: boolean | null;
  air_print_detect: boolean | null;
  firmware_idx_run: number | null;
  firmware_idx_sel: number | null;
}

export interface AmsSystemSettingSupports {
  insertion_update: boolean;
  power_on_update: boolean;
  remain_capacity: boolean;
  auto_switch_filament: boolean;
  air_print_detect: boolean;
  firmware_switch: boolean;
  reorder: boolean;
}

export interface AmsSettingsUnitInfo {
  ams_id: number;
  label: string;
}

export interface AmsSettingsFirmwareOption {
  idx: number;
  label: string;
}

export interface AmsSettingsGetResponse {
  state: AmsSystemSettingState;
  supports: AmsSystemSettingSupports;
  ams_units: AmsSettingsUnitInfo[];
  firmware_options: AmsSettingsFirmwareOption[];
}

export type AmsSettingsPostBody =
  | { action: 'user_setting'; startup_read_option: boolean; tray_read_option: boolean; calibrate_remain_flag: boolean }
  | { action: 'auto_switch_filament'; enabled: boolean }
  | { action: 'air_print_detect'; enabled: boolean }
  | { action: 'calibrate'; ams_id: number }
  | { action: 'firmware_switch'; firmware_idx: number }
  | { action: 'reorder' };

export interface AmsSettingsPostResponse {
  ok: boolean;
  sequence_id: string | null;
}

// Printer Settings dialog. Mirrors backend/app/schemas/printer_settings.py.
export interface AiDetectorStateOut {
  enabled: boolean | null;
  sensitivity: string | null;
}

export interface PrintOptionsState {
  auto_recovery: boolean | null;
  sound: boolean | null;
  filament_tangle: boolean | null;
  nozzle_blob: boolean | null;
  save_remote_to_storage: number | null;
  purify_air: number | null;
  open_door: number | null;
  plate_type: boolean | null;
  plate_align: boolean | null;
  snapshot: boolean | null;
  fod_check: boolean | null;
  displacement_detection: boolean | null;
  spaghetti_detector: AiDetectorStateOut;
  pileup_detector: AiDetectorStateOut;
  nozzleclumping_detector: AiDetectorStateOut;
  airprinting_detector: AiDetectorStateOut;
  first_layer_inspector: AiDetectorStateOut;
  ai_monitoring: AiDetectorStateOut;
}

export interface PrinterPartsState {
  nozzles: { id: number; type: string | null; diameter: number | null; flow_type: string | null }[];
}

export interface PrinterSettingsSupports {
  spaghetti_detector: boolean;
  pileup_detector: boolean;
  nozzleclumping_detector: boolean;
  airprinting_detector: boolean;
  first_layer_inspector: boolean;
  ai_monitoring: boolean;
  filament_tangle: boolean;
  nozzle_blob: boolean;
  fod_check: boolean;
  displacement_detection: boolean;
  open_door_check: boolean;
  purify_air: boolean;
  auto_recovery: boolean;
  sound: boolean;
  save_remote_to_storage: boolean;
  snapshot: boolean;
  plate_type: boolean;
  plate_align: boolean;
  parts_editable: boolean;
  parts_dual: boolean;
}

export interface PrinterSettingsGetResponse {
  print_options: PrintOptionsState;
  parts: PrinterPartsState;
  supports: PrinterSettingsSupports;
}

export type PrinterSettingsPostBody =
  | { action: 'print_option_bool';
      key: 'auto_recovery' | 'sound' | 'filament_tangle' | 'nozzle_blob' | 'plate_type' | 'plate_align';
      enabled: boolean }
  | { action: 'print_option_int';
      key: 'save_remote_to_storage' | 'purify_air' | 'open_door';
      value: number }
  | { action: 'xcam_control';
      module: 'first_layer_inspector' | 'spaghetti_detector' | 'purgechutepileup_detector'
            | 'nozzleclumping_detector' | 'airprinting_detector' | 'fod_check'
            | 'displacement_detection' | 'ai_monitoring';
      enabled: boolean;
      sensitivity: 'low' | 'medium' | 'high' | null }
  | { action: 'camera_snapshot'; enabled: boolean }
  | { action: 'set_nozzle'; nozzle_id: number; type: string; diameter: number; flow_type: string };

export interface PrinterSettingsPostResponse {
  ok: boolean;
  sequence_id: string | null;
}

// ---------- Filament Calibration (m062 / Plan 2) ----------

export type CaliMode =
  | 'pa_line'
  | 'pa_pattern'
  | 'pa_tower'
  | 'auto_pa_line'
  | 'flow_rate'
  | 'temp_tower'
  | 'vol_speed_tower'
  | 'vfa_tower'
  | 'retraction_tower';

export type CaliMethod = 'auto' | 'manual';

export type NozzleVolumeType = 'standard' | 'high_flow' | 'tpu_high_flow' | 'hybrid';

export type CalibModeState = 'disabled' | 'verification' | 'production';

export interface CalibCapabilities {
  pa_manual: boolean;
  flow_manual: boolean;
  temp_tower: boolean;
  vol_speed_tower: boolean;
  vfa_tower: boolean;
  retraction_tower: boolean;
  pa_auto: boolean;
  flow_auto: boolean;
  dual_extruder: boolean;
  extruders: Array<{ id: number; name: string }>;
  nozzles: Array<{
    id: number;
    diameter: number | null;
    type: string | null;
    flow_type: string | null;
  }>;
  // Per-mode lifecycle state — key is CaliMode, value is one of
  // 'disabled' / 'verification' / 'production'. Server mirrors the
  // MODE_STATE registry; frontend ANDs this with the capability flags
  // above to decide whether a row is interactive.
  mode_state: Record<string, CalibModeState>;
}

export interface CalibFilamentIn {
  ams_id: number;
  slot_id: number;
  tray_id: number;
  filament_id: string;
  filament_setting_id?: string | null;
  bed_temp: number;
  nozzle_temp: number;
  max_volumetric_speed: number;
  flow_rate?: number;
  extruder_id?: number | null;
}

export interface StartSessionIn {
  cali_mode: CaliMode;
  method: CaliMethod;
  nozzle_diameter: number;
  nozzle_volume_type: NozzleVolumeType;
  extruder_id?: number;
  filaments: CalibFilamentIn[];
  // Preset / slicer fields (mirror CalibSliceOnlyIn). Required for
  // manual modes that route through the slicer-sidecar pipeline
  // (PA Tower and beyond); ignored by AUTO modes (AUTO_PA_LINE,
  // FLOW_RATE fire MQTT directly).
  spec?: Record<string, number | string | boolean>;
  bundle?: SliceBundleSpec;
  printer_preset?: PresetRef;
  process_preset?: PresetRef;
  filament_presets?: PresetRef[];
  slicer?: 'orcaslicer' | 'bambu_studio';
  bed_type?: BedType;
  // Per-job dispatcher toggles. Same shape as PrintModal types' PrintOptions /
  // SwapMacrosOptions — the calibration backend forwards both onto the
  // resulting PrintQueueItem so the dispatcher fires swap macros / sets bed-
  // levelling / etc. just like a regular library job.
  print_options?: {
    bed_levelling: boolean;
    flow_cali: boolean;
    layer_inspect: boolean;
    timelapse: boolean;
    mesh_mode_fast_check: boolean;
    gcode_injection: boolean;
  };
  swap_macros?: {
    execute: boolean;
    events: Array<'swap_mode_start' | 'swap_mode_change_table'>;
  };
}

export interface CalibBakeOnlyIn {
  cali_mode: CaliMode;
  spec?: Record<string, number | string | boolean>;
  extruder_count?: number;
  pass_n?: number;
  bed_type?: BedType;
}

export interface CalibSliceOnlyIn {
  cali_mode: CaliMode;
  spec?: Record<string, number | string | boolean>;
  extruder_count?: number;
  pass_n?: number;
  // Either bundle OR (printer_preset + process_preset + filament_presets).
  // The server's validator rejects bodies that don't carry one shape or
  // the other. See SliceRequest for the symmetric pattern in the main
  // slice routes.
  bundle?: SliceBundleSpec;
  printer_preset?: PresetRef;
  process_preset?: PresetRef;
  filament_presets?: PresetRef[];
  slicer?: 'orcaslicer' | 'bambu_studio';
  bed_type?: BedType;
}

export interface CalibrationSessionOut {
  id: number;
  printer_id: number;
  user_id: number | null;
  cali_mode: string;
  method: string;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  extruder_id: number;
  status: 'running' | 'awaiting_user_input' | 'saved' | 'cancelled' | 'failed';
  stage: number;
  coarse_ratio: number | null;
  parent_session_id: number | null;
  mqtt_sequence_id: string | null;
  print_queue_item_id: number | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
}

export interface FilamentCalibrationOut {
  id: number;
  printer_id: number;
  filament_id: string;
  filament_setting_id: string | null;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  extruder_id: number;
  pa_k_value: number | null;
  pa_n_coef: number | null;
  flow_ratio: number | null;
  confidence: number | null;
  cali_mode: string;
  source: string;
  is_active: boolean;
  cali_idx: number | null;
  name: string;
  notes: string | null;
  nozzle_id: string | null;
  calibrated_by_user_id: number | null;
  created_at: string;
}

export interface ManualResultIn {
  best_line_index?: number;
  pa_k_value?: number;
  coarse_modifier?: number;
  skip_fine?: boolean;
  fine_modifier?: number;
}

export interface ManualResultOut {
  saved_rows: FilamentCalibrationOut[];
  next_session_id: number | null;
}

export interface ExtrusionCaliResultOut {
  tray_id: number;
  ams_id: number;
  slot_id: number;
  extruder_id: number;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  filament_id: string;
  setting_id: string;
  k_value: number;
  n_coef: number;
  confidence: number;
  nozzle_pos_id: number;
  nozzle_sn: string;
}

export interface AutoResultEditIn {
  tray_id: number;
  k_value?: number;
  n_coef?: number;
  flow_ratio?: number;
  name?: string;
  save?: boolean;
}

export interface AutoResultIn {
  results: AutoResultEditIn[];
}

export interface PACalibHistoryEntryOut {
  cali_idx: number;
  name: string;
  filament_id: string;
  setting_id: string;
  nozzle_diameter: number;
  nozzle_volume_type: string;
  extruder_id: number;
  k_value: number;
  n_coef: number;
}

// API functions
export const api = {
  // Authentication
  getAuthStatus: () => request<AuthStatus>('/auth/status'),
  getEncryptionStatus: () => request<EncryptionStatus>('/auth/encryption-status'),
  setupAuth: (data: SetupRequest) =>
    request<SetupResponse>('/auth/setup', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  login: (data: LoginRequest) =>
    request<LoginResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  logout: () =>
    request<{ message: string }>('/auth/logout', {
      method: 'POST',
    }),
  getCurrentUser: () => request<UserResponse>('/auth/me'),

  // Advanced Authentication
  testSMTP: (data: TestSMTPRequest) =>
    request<TestSMTPResponse>('/auth/smtp/test', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  getSMTPSettings: () => request<SMTPSettings | null>('/auth/smtp'),
  saveSMTPSettings: (data: SMTPSettings) =>
    request<{ message: string }>('/auth/smtp', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  enableAdvancedAuth: () =>
    request<{ message: string; advanced_auth_enabled: boolean }>('/auth/advanced-auth/enable', {
      method: 'POST',
    }),
  disableAdvancedAuth: () =>
    request<{ message: string; advanced_auth_enabled: boolean }>('/auth/advanced-auth/disable', {
      method: 'POST',
    }),
  getAdvancedAuthStatus: () => request<AdvancedAuthStatus>('/auth/advanced-auth/status'),
  // LDAP Authentication
  getLDAPStatus: () => request<LDAPStatus>('/auth/ldap/status'),
  testLDAP: () =>
    request<LDAPTestResponse>('/auth/ldap/test', {
      method: 'POST',
    }),
  forgotPassword: (data: ForgotPasswordRequest) =>
    request<ForgotPasswordResponse>('/auth/forgot-password', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  // H-6: confirm password reset using the token from the emailed link
  forgotPasswordConfirm: (token: string, newPassword: string) =>
    request<ForgotPasswordResponse>('/auth/forgot-password/confirm', {
      method: 'POST',
      body: JSON.stringify({ token, new_password: newPassword }),
    }),
  resetUserPassword: (data: ResetPasswordRequest) =>
    request<ResetPasswordResponse>('/auth/reset-password', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  // 2FA (§18.1)
  get2FAStatus: () => request<TwoFAStatus>('/auth/2fa/status'),
  setupTOTP: () => request<TOTPSetupResponse>('/auth/2fa/totp/setup', { method: 'POST' }),
  enableTOTP: (code: string) =>
    request<TOTPEnableResponse>('/auth/2fa/totp/enable', {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
  disableTOTP: (code: string) =>
    request<{ message: string }>('/auth/2fa/totp/disable', {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
  regenerateBackupCodes: (code: string) =>
    request<BackupCodesResponse>('/auth/2fa/totp/regenerate-backup-codes', {
      method: 'POST',
      body: JSON.stringify({ code }),
    }),
  enableEmailOTP: () =>
    request<{ message: string; setup_token: string }>('/auth/2fa/email/enable', { method: 'POST' }),
  confirmEnableEmailOTP: (setup_token: string, code: string) =>
    request<{ message: string }>('/auth/2fa/email/enable/confirm', {
      method: 'POST',
      body: JSON.stringify({ setup_token, code }),
    }),
  disableEmailOTP: (password: string) =>
    request<{ message: string }>('/auth/2fa/email/disable', {
      method: 'POST',
      body: JSON.stringify({ password }),
    }),
  sendEmailOTP: (preAuthToken: string) =>
    request<{ message: string; pre_auth_token?: string }>('/auth/2fa/email/send', {
      method: 'POST',
      body: JSON.stringify({ pre_auth_token: preAuthToken }),
    }),
  verify2FA: (data: TwoFAVerifyRequest) =>
    request<LoginResponse>('/auth/2fa/verify', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  admin2FADisable: (userId: number) =>
    request<{ message: string }>(`/auth/2fa/admin/${userId}`, { method: 'DELETE' }),

  // OIDC (§18.2)
  getOIDCProviders: () => request<OIDCProvider[]>('/auth/oidc/providers'),
  getOIDCProvidersAll: () => request<OIDCProvider[]>('/auth/oidc/providers/all'),
  createOIDCProvider: (data: OIDCProviderCreate) =>
    request<OIDCProvider>('/auth/oidc/providers', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateOIDCProvider: (id: number, data: Partial<OIDCProviderCreate>) =>
    request<OIDCProvider>(`/auth/oidc/providers/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteOIDCProvider: (id: number) =>
    request<{ message: string }>(`/auth/oidc/providers/${id}`, { method: 'DELETE' }),
  getOIDCAuthorizeUrl: (providerId: number) =>
    request<{ auth_url: string }>(`/auth/oidc/authorize/${providerId}`),
  exchangeOIDCToken: (oidcToken: string) =>
    request<LoginResponse>('/auth/oidc/exchange', {
      method: 'POST',
      body: JSON.stringify({ oidc_token: oidcToken }),
    }),
  getOIDCLinks: () => request<OIDCLink[]>('/auth/oidc/links'),
  deleteOIDCLink: (providerId: number) =>
    request<{ message: string }>(`/auth/oidc/links/${providerId}`, { method: 'DELETE' }),

  // Users
  getUsers: () => request<UserResponse[]>('/users/'),
  getUser: (id: number) => request<UserResponse>(`/users/${id}`),
  createUser: (data: UserCreate) =>
    request<UserResponse>('/users/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateUser: (id: number, data: UserUpdate) =>
    request<UserResponse>(`/users/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteUser: (id: number, deleteItems: boolean = false) =>
    request<void>(`/users/${id}?delete_items=${deleteItems}`, {
      method: 'DELETE',
    }),
  getUserItemsCount: (id: number) =>
    request<{ archives: number; queue_items: number; library_files: number }>(`/users/${id}/items-count`),
  changePassword: (currentPassword: string, newPassword: string) =>
    request<{ message: string }>('/users/me/change-password', {
      method: 'POST',
      body: JSON.stringify({ current_password: currentPassword, new_password: newPassword }),
    }),

  // User Email Notifications
  getUserEmailPreferences: () =>
    request<UserEmailPreferences>('/user-notifications/preferences'),
  updateUserEmailPreferences: (data: UserEmailPreferences) =>
    request<UserEmailPreferences>('/user-notifications/preferences', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  // Groups
  getPermissions: () => request<PermissionsListResponse>('/groups/permissions'),
  getGroups: () => request<Group[]>('/groups/'),
  getGroup: (id: number) => request<GroupDetail>(`/groups/${id}`),
  createGroup: (data: GroupCreate) =>
    request<Group>('/groups/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateGroup: (id: number, data: GroupUpdate) =>
    request<Group>(`/groups/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteGroup: (id: number) =>
    request<void>(`/groups/${id}`, {
      method: 'DELETE',
    }),
  addUserToGroup: (groupId: number, userId: number) =>
    request<void>(`/groups/${groupId}/users/${userId}`, {
      method: 'POST',
    }),
  removeUserFromGroup: (groupId: number, userId: number) =>
    request<void>(`/groups/${groupId}/users/${userId}`, {
      method: 'DELETE',
    }),

  // Printers
  getPrinters: () => request<Printer[]>('/printers/'),
  getPrinter: (id: number) => request<Printer>(`/printers/${id}`),
  createPrinter: (data: PrinterCreate) =>
    request<Printer>('/printers/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updatePrinter: (id: number, data: Partial<PrinterCreate>) =>
    request<Printer>(`/printers/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deletePrinter: (id: number, deleteArchives: boolean = true) =>
    request<{ status: string; archives_deleted: boolean }>(
      `/printers/${id}?delete_archives=${deleteArchives}`,
      { method: 'DELETE' }
    ),
  getDeveloperModeWarnings: () =>
    request<{ printer_id: number; name: string }[]>('/printers/developer-mode-warnings'),
  getAvailableFilaments: (model: string, location?: string) => {
    const params = new URLSearchParams({ model });
    if (location) params.set('location', location);
    return request<Array<{ type: string; color: string; tray_info_idx: string; tray_sub_brands: string; extruder_id: number | null }>>(`/printers/available-filaments?${params}`);
  },
  getPrinterStatus: (id: number) =>
    request<PrinterStatus>(`/printers/${id}/status`),
  refreshPrinterStatus: (id: number) =>
    request<{ status: string }>(`/printers/${id}/refresh-status`, {
      method: 'POST',
    }),
  connectPrinter: (id: number) =>
    request<{ connected: boolean }>(`/printers/${id}/connect`, {
      method: 'POST',
    }),
  disconnectPrinter: (id: number) =>
    request<{ connected: boolean }>(`/printers/${id}/disconnect`, {
      method: 'POST',
    }),
  testExternalCamera: (printerId: number, url: string, cameraType: string) =>
    request<{ success: boolean; error?: string; resolution?: string }>(
      `/printers/${printerId}/camera/external/test?url=${encodeURIComponent(url)}&camera_type=${encodeURIComponent(cameraType)}`,
      { method: 'POST' }
    ),

  // Print Control
  stopPrint: (printerId: number) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/print/stop`, {
      method: 'POST',
    }),
  pausePrint: (printerId: number) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/print/pause`, {
      method: 'POST',
    }),
  resumePrint: (printerId: number) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/print/resume`, {
      method: 'POST',
    }),
  clearPlate: (printerId: number) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/clear-plate`, {
      method: 'POST',
    }),
  startCalibration: (printerId: number, options: {
    bed_leveling?: boolean;
    vibration?: boolean;
    motor_noise?: boolean;
    nozzle_offset?: boolean;
    high_temp_heatbed?: boolean;
  }) => {
    const params = new URLSearchParams();
    Object.entries(options).forEach(([k, v]) => { if (v) params.set(k, 'true'); });
    return request<{ success: boolean }>(`/printers/${printerId}/calibration?${params}`, { method: 'POST' });
  },

  // Get current print user (for reprint tracking - Issue #206)
  getCurrentPrintUser: (printerId: number) =>
    request<{ user_id?: number; username?: string }>(`/printers/${printerId}/current-print-user`),

  // Print Speed Control
  setPrintSpeed: (printerId: number, mode: number) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/print-speed?mode=${mode}`, {
      method: 'POST',
    }),

  // Bed (Z-axis) jog
  bedJog: (printerId: number, distance: number, force: boolean = false) =>
    request<{ success: boolean; message: string }>(
      `/printers/${printerId}/bed-jog?distance=${distance}&force=${force}`,
      { method: 'POST' },
    ),
  homeAxes: (printerId: number, axes: 'z' | 'xy' | 'all' = 'z') =>
    request<{ success: boolean; message: string }>(
      `/printers/${printerId}/home-axes?axes=${axes}`,
      { method: 'POST' },
    ),

  // Chamber Light Control
  setChamberLight: (printerId: number, on: boolean) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/chamber-light?on=${on}`, {
      method: 'POST',
    }),

  // AMS Drying Control
  startDrying: (printerId: number, amsId: number, temp: number, duration: number, filament: string = '', rotateTray: boolean = false) =>
    request<{ status: string; ams_id: number; temp: number; duration: number }>(
      `/printers/${printerId}/drying/start?ams_id=${amsId}&temp=${temp}&duration=${duration}&filament=${encodeURIComponent(filament)}&rotate_tray=${rotateTray}`,
      { method: 'POST' }
    ),
  stopDrying: (printerId: number, amsId: number) =>
    request<{ status: string; ams_id: number }>(
      `/printers/${printerId}/drying/stop?ams_id=${amsId}`,
      { method: 'POST' }
    ),

  // Skip Objects
  getPrintableObjects: (printerId: number) =>
    request<{
      objects: Array<{ id: number; name: string; x: number | null; y: number | null; skipped: boolean }>;
      total: number;
      skipped_count: number;
      is_printing: boolean;
      bbox_all: [number, number, number, number] | null;
    }>(`/printers/${printerId}/print/objects`),

  skipObjects: (printerId: number, objectIds: number[]) =>
    request<{ success: boolean; message: string; skipped_objects: number[] }>(
      `/printers/${printerId}/print/skip-objects`,
      {
        method: 'POST',
        body: JSON.stringify(objectIds),
      }
    ),

  // HMS Errors
  clearHMSErrors: (printerId: number) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/hms/clear`, { method: 'POST' }),

  // AMS Control
  refreshAmsSlot: (printerId: number, amsId: number, slotId: number) =>
    request<{ success: boolean; message: string }>(
      `/printers/${printerId}/ams/${amsId}/slot/${slotId}/refresh`,
      { method: 'POST' }
    ),

  // AMS load/unload (#891) — granular ams_change_filament primitives.
  // tray_id semantics: 0..15 = AMS slot, 254 = external spool / Ext-L, 255 = Ext-R (H2D).
  amsLoadFilament: (printerId: number, trayId: number) =>
    request<{ success: boolean; tray_id: number }>(
      `/printers/${printerId}/ams/load?tray_id=${trayId}`,
      { method: 'POST' }
    ),
  amsUnloadFilament: (printerId: number) =>
    request<{ success: boolean }>(
      `/printers/${printerId}/ams/unload`,
      { method: 'POST' }
    ),

  // AMS Settings dialog (BS port). Mirrors backend/app/schemas/ams_settings.py
  // — keep field names in sync.
  getAmsSettings: (printerId: number) =>
    request<AmsSettingsGetResponse>(`/printers/${printerId}/ams/settings`),
  postAmsSettings: (printerId: number, body: AmsSettingsPostBody) =>
    request<AmsSettingsPostResponse>(`/printers/${printerId}/ams/settings`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  getPrinterSettings: (printerId: number) =>
    request<PrinterSettingsGetResponse>(`/printers/${printerId}/settings`),
  postPrinterSettings: (printerId: number, body: PrinterSettingsPostBody) =>
    request<PrinterSettingsPostResponse>(`/printers/${printerId}/settings`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),

  // Filament Calibration (m062 / Plan 2)
  getCalibrationCapabilities: (printerId: number) =>
    request<CalibCapabilities>(`/printers/${printerId}/calibration/capabilities`),
  startCalibrationSession: (printerId: number, body: StartSessionIn) =>
    request<CalibrationSessionOut>(`/printers/${printerId}/calibration/sessions`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  bakeCalibrationForVerification: async (
    printerId: number,
    body: CalibBakeOnlyIn,
  ): Promise<{ blob: Blob; filename: string }> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const response = await fetch(`${API_BASE}/printers/${printerId}/calibration/bake-only`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      const message = typeof err === 'object' && err
        ? (err.detail?.message ?? err.detail?.detail ?? err.detail ?? err.message ?? `HTTP ${response.status}`)
        : `HTTP ${response.status}`;
      throw new Error(String(message));
    }
    let filename = `calibration_${body.cali_mode}.bake.3mf`;
    const dispo = response.headers.get('Content-Disposition');
    if (dispo) {
      const utf8Match = dispo.match(/filename\*=UTF-8''([^;]+)/i);
      if (utf8Match) filename = decodeURIComponent(utf8Match[1]);
      else {
        const plain = dispo.match(/filename="?([^"]+)"?/);
        if (plain) filename = plain[1];
      }
    }
    const blob = await response.blob();
    return { blob, filename };
  },
  sliceCalibrationForVerification: async (
    printerId: number,
    body: CalibSliceOnlyIn,
  ): Promise<{ blob: Blob; filename: string }> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const response = await fetch(`${API_BASE}/printers/${printerId}/calibration/slice-only`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const err = await response.json().catch(() => ({}));
      const message = typeof err === 'object' && err
        ? (err.detail?.message ?? err.detail?.detail ?? err.detail ?? err.message ?? `HTTP ${response.status}`)
        : `HTTP ${response.status}`;
      throw new Error(String(message));
    }
    let filename = `calibration_${body.cali_mode}.gcode.3mf`;
    const dispo = response.headers.get('Content-Disposition');
    if (dispo) {
      const utf8Match = dispo.match(/filename\*=UTF-8''([^;]+)/i);
      if (utf8Match) filename = decodeURIComponent(utf8Match[1]);
      else {
        const plain = dispo.match(/filename="?([^"]+)"?/);
        if (plain) filename = plain[1];
      }
    }
    const blob = await response.blob();
    return { blob, filename };
  },
  getCalibrationSession: (sessionId: number) =>
    request<CalibrationSessionOut>(`/calibration/sessions/${sessionId}`),
  cancelCalibrationSession: (sessionId: number) =>
    request<void>(`/calibration/sessions/${sessionId}/cancel`, { method: 'POST' }),
  submitManualResult: (sessionId: number, body: ManualResultIn) =>
    request<ManualResultOut>(`/calibration/sessions/${sessionId}/manual-result`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  listAwaitingSessions: (printerId: number) =>
    request<CalibrationSessionOut[]>(
      `/calibration/sessions?printer_id=${printerId}&status=awaiting_user_input`,
    ),
  // All sessions for a printer that are still "live" (running or
  // awaiting user input). Used by the wizard's resume-banner to
  // surface an in-progress calibration after a page reload — the
  // operator sees "Resume / Discard" and the modal jumps straight
  // to the right step.
  listActiveSessions: (printerId: number) =>
    request<CalibrationSessionOut[]>(
      `/calibration/sessions?printer_id=${printerId}`,
    ),
  submitAutoResult: (sessionId: number, body: AutoResultIn) =>
    request<FilamentCalibrationOut[]>(`/calibration/sessions/${sessionId}/auto-result`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getCalibrationAutoResults: (printerId: number) =>
    request<ExtrusionCaliResultOut[]>(`/printers/${printerId}/calibration/auto-results`),
  listFilamentCalibrations: (params: {
    printer_id?: number;
    filament_id?: string;
    nozzle_diameter?: number;
    is_active?: boolean;
  }) => {
    const q = new URLSearchParams();
    for (const [k, v] of Object.entries(params)) {
      if (v != null) q.append(k, String(v));
    }
    return request<FilamentCalibrationOut[]>(`/filament-calibrations?${q.toString()}`);
  },
  setActiveCalibration: (caliId: number) =>
    request<FilamentCalibrationOut>(`/filament-calibrations/${caliId}/set-active`, {
      method: 'POST',
    }),
  deleteCalibration: (caliId: number) =>
    request<void>(`/filament-calibrations/${caliId}`, { method: 'DELETE' }),
  getPrinterCalibrationHistory: (printerId: number) =>
    request<PACalibHistoryEntryOut[]>(`/printers/${printerId}/calibration/history`),
  refreshPrinterCalibrationHistory: (
    printerId: number,
    nozzle_diameter: number,
    extruder_id: number = 0,
  ) =>
    request<{ sequence_id: string }>(
      `/printers/${printerId}/calibration/history/refresh?nozzle_diameter=${nozzle_diameter}&extruder_id=${extruder_id}`,
      { method: 'POST' },
    ),

  // MQTT Debug Logging
  enableMQTTLogging: (printerId: number) =>
    request<{ logging_enabled: boolean }>(`/printers/${printerId}/logging/enable`, {
      method: 'POST',
    }),
  disableMQTTLogging: (printerId: number) =>
    request<{ logging_enabled: boolean }>(`/printers/${printerId}/logging/disable`, {
      method: 'POST',
    }),
  getMQTTLogs: (printerId: number) =>
    request<MQTTLogsResponse>(`/printers/${printerId}/logging`),
  clearMQTTLogs: (printerId: number) =>
    request<{ status: string }>(`/printers/${printerId}/logging`, {
      method: 'DELETE',
    }),

  // Printer File Manager
  getPrinterFiles: (printerId: number, path = '/') =>
    request<{
      path: string;
      files: Array<{
        name: string;
        is_directory: boolean;
        size: number;
        path: string;
        mtime?: string;
      }>;
    }>(`/printers/${printerId}/files?path=${encodeURIComponent(path)}`),
  getPrinterFileDownloadUrl: (printerId: number, path: string) =>
    `${API_BASE}/printers/${printerId}/files/download?path=${encodeURIComponent(path)}`,
  getPrinterFileGcodeUrl: (printerId: number, path: string) =>
    `${API_BASE}/printers/${printerId}/files/gcode?path=${encodeURIComponent(path)}`,
  getPrinterFilePlates: (printerId: number, path: string) =>
    request<{
      printer_id: number;
      path: string;
      filename: string;
      plates: Array<{
        index: number;
        name: string | null;
        objects: string[];
        has_thumbnail: boolean;
        thumbnail_url: string | null;
        print_time_seconds: number | null;
        filament_used_grams: number | null;
        filaments: Array<{
          slot_id: number;
          type: string;
          color: string;
          used_grams: number;
          used_meters: number;
        }>;
      }>;
      is_multi_plate: boolean;
    }>(`/printers/${printerId}/files/plates?path=${encodeURIComponent(path)}`),
  getPrinterFilePlateThumbnail: (printerId: number, plateIndex: number, path: string) =>
    `${API_BASE}/printers/${printerId}/files/plate-thumbnail/${plateIndex}?path=${encodeURIComponent(path)}`,
  downloadPrinterFile: async (printerId: number, path: string): Promise<void> => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(
      `${API_BASE}/printers/${printerId}/files/download?path=${encodeURIComponent(path)}`,
      { headers }
    );
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get('Content-Disposition');
    const filename = parseContentDispositionFilename(disposition) || path.split('/').pop() || 'download';
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },
  downloadPrinterFilesAsZip: async (printerId: number, paths: string[]): Promise<Blob> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/printers/${printerId}/files/download-zip`, {
      method: 'POST',
      headers,
      body: JSON.stringify({ paths }),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.blob();
  },
  deletePrinterFile: (printerId: number, path: string) =>
    request<{ status: string; path: string }>(`/printers/${printerId}/files?path=${encodeURIComponent(path)}`, {
      method: 'DELETE',
    }),
  importPrinterFilesToLibrary: (
    printerId: number,
    paths: string[],
    folderId: number | null,
  ) =>
    request<{
      imported: Array<{
        path: string;
        library_file_id: number;
        filename: string;
        was_existing: boolean;
      }>;
      skipped: Array<{ path: string; reason: string; detail?: string }>;
    }>(`/printers/${printerId}/files/import-to-library`, {
      method: 'POST',
      body: JSON.stringify({ paths, folder_id: folderId }),
    }),
  getPrinterStorage: (printerId: number) =>
    request<{ used_bytes: number | null; free_bytes: number | null }>(`/printers/${printerId}/storage`),

  // Archives
  getArchives: (params: ArchiveListParams = {}) => {
    const qs = new URLSearchParams();
    if (params.all) qs.set('all', 'true');
    if (params.page && !params.all) qs.set('page', String(params.page));
    if (params.per_page && !params.all) qs.set('per_page', String(params.per_page));
    if (params.printer_id) qs.set('printer_id', String(params.printer_id));
    if (params.project_id) qs.set('project_id', String(params.project_id));
    if (params.date_from) qs.set('date_from', params.date_from);
    if (params.date_to) qs.set('date_to', params.date_to);
    if (params.search) qs.set('search', params.search);
    if (params.collection) qs.set('collection', params.collection);
    if (params.material) qs.set('material', params.material);
    if (params.colors) qs.set('colors', params.colors);
    if (params.color_mode && params.color_mode !== 'or') qs.set('color_mode', params.color_mode);
    if (params.favorites_only) qs.set('favorites_only', 'true');
    if (params.hide_failed) qs.set('hide_failed', 'true');
    if (params.hide_duplicates) qs.set('hide_duplicates', 'true');
    if (params.tag) qs.set('tag', params.tag);
    if (params.kind) qs.set('kind', params.kind);
    if (params.sort_by) qs.set('sort_by', params.sort_by);
    return request<PaginatedArchiveResponse>(`/archives/?${qs}`);
  },
  getArchiveFilterOptions: () => request<ArchiveFilterOptions>('/archives/filter-options'),
  getArchiveCleanupStatus: () => request<{
    enabled: boolean;
    days: number;
    last_run: {
      started_at: string;
      finished_at: string | null;
      groups_scanned: number;
      groups_skipped_active_print: number;
      groups_skipped_queue: number;
      groups_skipped_library: number;
      groups_cleared: number;
      archives_cleared: number;
      bytes_freed: number;
      errors: string[];
    } | null;
    next_run_at: string | null;
  }>('/archives/cleanup/status'),
  getArchiveCleanupPreview: (overrideDays?: number) => request<{
    enabled: boolean;
    days: number;
    cutoff?: string;
    groups: number;
    archives: number;
    bytes: number;
  }>(overrideDays ? `/archives/cleanup/preview?days=${overrideDays}` : '/archives/cleanup/preview'),
  runArchiveCleanup: (overrideDays?: number) => request<{
    started_at: string;
    finished_at: string | null;
    groups_scanned: number;
    groups_skipped_active_print: number;
    groups_skipped_queue: number;
    groups_skipped_library: number;
    groups_cleared: number;
    archives_cleared: number;
    bytes_freed: number;
    errors: string[];
  }>(overrideDays ? `/archives/cleanup/run?days=${overrideDays}` : '/archives/cleanup/run', { method: 'POST' }),
  getArchivesSlim: (dateFrom?: string, dateTo?: string) => {
    const params = new URLSearchParams();
    if (dateFrom) params.set('date_from', dateFrom);
    if (dateTo) params.set('date_to', dateTo);
    const qs = params.toString();
    return request<ArchiveSlim[]>(`/archives/slim${qs ? `?${qs}` : ''}`);
  },
  getArchive: (id: number) => request<Archive>(`/archives/${id}`),
  searchArchives: (query: string, options?: {
    printerId?: number;
    projectId?: number;
    status?: string;
    limit?: number;
    offset?: number;
  }) => {
    const params = new URLSearchParams();
    params.set('q', query);
    if (options?.printerId) params.set('printer_id', String(options.printerId));
    if (options?.projectId) params.set('project_id', String(options.projectId));
    if (options?.status) params.set('status', options.status);
    if (options?.limit) params.set('limit', String(options.limit));
    if (options?.offset) params.set('offset', String(options.offset));
    return request<Archive[]>(`/archives/search?${params}`);
  },
  rebuildSearchIndex: () => request<{ message: string }>('/archives/search/rebuild-index', { method: 'POST' }),
  updateArchive: (id: number, data: {
    printer_id?: number | null;
    project_id?: number | null;
    print_name?: string;
    is_favorite?: boolean;
    tags?: string;
    notes?: string;
    cost?: number;
    failure_reason?: string | null;
    status?: string;
    quantity?: number;
    external_url?: string | null;
  }) =>
    request<Archive>(`/archives/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  toggleFavorite: (id: number) =>
    request<Archive>(`/archives/${id}/favorite`, { method: 'POST' }),
  deleteArchive: (id: number) =>
    request<void>(`/archives/${id}`, { method: 'DELETE' }),
  getArchiveStats: (options?: { dateFrom?: string; dateTo?: string }) => {
    const params = new URLSearchParams();
    if (options?.dateFrom) params.set('date_from', options.dateFrom);
    if (options?.dateTo) params.set('date_to', options.dateTo);
    const qs = params.toString();
    return request<ArchiveStats>(`/archives/stats${qs ? `?${qs}` : ''}`);
  },
  // Tag management
  getTags: () => request<TagInfo[]>('/archives/tags'),
  renameTag: (oldName: string, newName: string) =>
    request<{ affected: number }>(`/archives/tags/${encodeURIComponent(oldName)}`, {
      method: 'PUT',
      body: JSON.stringify({ new_name: newName }),
    }),
  deleteTag: (name: string) =>
    request<{ affected: number }>(`/archives/tags/${encodeURIComponent(name)}`, {
      method: 'DELETE',
    }),
  recalculateCosts: () =>
    request<{ message: string; updated: number }>('/archives/recalculate-costs', { method: 'POST' }),
  getFailureAnalysis: (options?: { days?: number; dateFrom?: string; dateTo?: string; printerId?: number; projectId?: number }) => {
    const params = new URLSearchParams();
    if (options?.days) params.set('days', String(options.days));
    if (options?.dateFrom) params.set('date_from', options.dateFrom);
    if (options?.dateTo) params.set('date_to', options.dateTo);
    if (options?.printerId) params.set('printer_id', String(options.printerId));
    if (options?.projectId) params.set('project_id', String(options.projectId));
    const qs = params.toString();
    return request<FailureAnalysis>(`/archives/analysis/failures${qs ? `?${qs}` : ''}`);
  },
  compareArchives: (archiveIds: number[]) =>
    request<ArchiveComparison>(`/archives/compare?archive_ids=${archiveIds.join(',')}`),
  findSimilarArchives: (archiveId: number, limit = 10) =>
    request<SimilarArchive[]>(`/archives/${archiveId}/similar?limit=${limit}`),
  exportArchives: async (options?: {
    format?: 'csv' | 'xlsx';
    fields?: string[];
    printerId?: number;
    projectId?: number;
    status?: string;
    dateFrom?: string;
    dateTo?: string;
    search?: string;
  }): Promise<{ blob: Blob; filename: string }> => {
    const params = new URLSearchParams();
    if (options?.format) params.set('format', options.format);
    if (options?.fields) params.set('fields', options.fields.join(','));
    if (options?.printerId) params.set('printer_id', String(options.printerId));
    if (options?.projectId) params.set('project_id', String(options.projectId));
    if (options?.status) params.set('status', options.status);
    if (options?.dateFrom) params.set('date_from', options.dateFrom);
    if (options?.dateTo) params.set('date_to', options.dateTo);
    if (options?.search) params.set('search', options.search);

    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/export?${params}`, { headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }

    const contentDisposition = response.headers.get('Content-Disposition');
    let filename = options?.format === 'xlsx' ? 'archives_export.xlsx' : 'archives_export.csv';
    if (contentDisposition) {
      const match = contentDisposition.match(/filename="?([^"]+)"?/);
      if (match) filename = match[1];
    }

    const blob = await response.blob();
    return { blob, filename };
  },
  exportStats: async (options?: {
    format?: 'csv' | 'xlsx';
    days?: number;
    printerId?: number;
    projectId?: number;
  }): Promise<{ blob: Blob; filename: string }> => {
    const params = new URLSearchParams();
    if (options?.format) params.set('format', options.format);
    if (options?.days) params.set('days', String(options.days));
    if (options?.printerId) params.set('printer_id', String(options.printerId));
    if (options?.projectId) params.set('project_id', String(options.projectId));

    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/stats/export?${params}`, { headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }

    const contentDisposition = response.headers.get('Content-Disposition');
    let filename = options?.format === 'xlsx' ? 'stats_export.xlsx' : 'stats_export.csv';
    if (contentDisposition) {
      const match = contentDisposition.match(/filename="?([^"]+)"?/);
      if (match) filename = match[1];
    }

    const blob = await response.blob();
    return { blob, filename };
  },
  getArchiveDuplicates: (id: number) =>
    request<{ duplicates: ArchiveDuplicate[]; count: number }>(`/archives/${id}/duplicates`),
  backfillContentHashes: () =>
    request<{ updated: number; errors: Array<{ id: number; error: string }> }>('/archives/backfill-hashes', {
      method: 'POST',
    }),
  // Stable URL so the browser can cache the thumbnail between renders —
  // pass ``version`` (e.g. archive.created_at) to force a miss after the
  // bytes actually changed. The old ``?v=Date.now()`` footgun made every
  // re-render re-fetch every thumbnail, which turned any background tick
  // (dispatch progress, toast updates) into a thumbnail thrashing storm.
  getArchiveThumbnail: (id: number, version?: string | number) =>
    `${API_BASE}/archives/${id}/thumbnail${version ? `?v=${encodeURIComponent(String(version))}` : ''}`,
  getArchivePlateThumbnail: (id: number, plateIndex: number) =>
    `${API_BASE}/archives/${id}/plate-thumbnail/${plateIndex}`,
  getArchiveDownload: (id: number) => `${API_BASE}/archives/${id}/download`,
  downloadArchive: async (id: number, filename?: string): Promise<void> => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${id}/download`, { headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get('Content-Disposition');
    const downloadFilename = parseContentDispositionFilename(disposition) || filename || `archive_${id}.3mf`;
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = downloadFilename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },
  getArchiveGcode: (id: number) => `${API_BASE}/archives/${id}/gcode`,
  getArchivePlatePreview: (id: number) => `${API_BASE}/archives/${id}/plate-preview`,
  // Same cache-stability policy as ``getArchiveThumbnail``.
  getArchiveTimelapse: (id: number, version?: string | number) =>
    `${API_BASE}/archives/${id}/timelapse${version ? `?v=${encodeURIComponent(String(version))}` : ''}`,
  scanArchiveTimelapse: (id: number) =>
    request<{
      status: string;
      message: string;
      filename?: string;
      available_files?: Array<{ name: string; path: string; size: number; mtime: string | null }>;
    }>(`/archives/${id}/timelapse/scan`, {
      method: 'POST',
    }),
  selectArchiveTimelapse: (id: number, filename: string) =>
    request<{ status: string; message: string; filename: string }>(
      `/archives/${id}/timelapse/select?filename=${encodeURIComponent(filename)}`,
      { method: 'POST' }
    ),
  deleteArchiveTimelapse: (id: number) =>
    request<{ status: string }>(`/archives/${id}/timelapse`, {
      method: 'DELETE',
    }),
  uploadArchiveTimelapse: async (archiveId: number, file: File): Promise<{ status: string; filename: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${archiveId}/timelapse/upload`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  // Timelapse Editor
  getTimelapseInfo: (archiveId: number) =>
    request<{
      duration: number;
      width: number;
      height: number;
      fps: number;
      codec: string;
      file_size: number;
      has_audio: boolean;
    }>(`/archives/${archiveId}/timelapse/info`),
  getTimelapseThumbnails: (archiveId: number, count: number = 10) =>
    request<{
      thumbnails: string[];
      timestamps: number[];
    }>(`/archives/${archiveId}/timelapse/thumbnails?count=${count}`),
  processTimelapse: async (
    archiveId: number,
    params: {
      trimStart?: number;
      trimEnd?: number;
      speed?: number;
      saveMode: 'replace' | 'new';
      outputFilename?: string;
    },
    audioFile?: File
  ): Promise<{ status: string; output_path: string | null; message: string }> => {
    const formData = new FormData();
    formData.append('trim_start', String(params.trimStart ?? 0));
    if (params.trimEnd !== undefined) {
      formData.append('trim_end', String(params.trimEnd));
    }
    formData.append('speed', String(params.speed ?? 1));
    formData.append('save_mode', params.saveMode);
    if (params.outputFilename) {
      formData.append('output_filename', params.outputFilename);
    }
    if (audioFile) {
      formData.append('audio', audioFile);
    }
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${archiveId}/timelapse/process`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  // Photos
  getArchivePhotoUrl: (archiveId: number, filename: string) =>
    `${API_BASE}/archives/${archiveId}/photos/${encodeURIComponent(filename)}`,
  uploadArchivePhoto: async (archiveId: number, file: File): Promise<{ status: string; filename: string; photos: string[] }> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${archiveId}/photos`, {
      headers,
      method: 'POST',
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  deleteArchivePhoto: (archiveId: number, filename: string) =>
    request<{ status: string; photos: string[] | null }>(`/archives/${archiveId}/photos/${encodeURIComponent(filename)}`, {
      method: 'DELETE',
    }),
  // Source 3MF (original slicer project file)
  getSource3mfDownloadUrl: (archiveId: number) =>
    `${API_BASE}/archives/${archiveId}/source`,
  downloadSource3mf: async (archiveId: number): Promise<void> => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${archiveId}/source`, { headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get('Content-Disposition');
    const filename = parseContentDispositionFilename(disposition) || `source_${archiveId}.3mf`;
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },
  getSource3mfForSlicer: (archiveId: number, filename: string) => {
    // Sanitize: slicers url_decode() the entire URL, so / \ ? # in filenames break path routing
    const safe = filename.replace(/[/\\?#]/g, '_');
    return `${API_BASE}/archives/${archiveId}/source/${encodeURIComponent(safe.endsWith('.3mf') ? safe : safe + '.3mf')}`;
  },
  createSourceSlicerToken: (archiveId: number) =>
    request<{ token: string }>(`/archives/${archiveId}/source-slicer-token`, { method: 'POST' }),
  getSourceSlicerDownloadUrl: (archiveId: number, token: string, filename: string) => {
    const safe = filename.replace(/[/\\?#]/g, '_');
    return `${API_BASE}/archives/${archiveId}/source-dl/${token}/${encodeURIComponent(safe.endsWith('.3mf') ? safe : safe + '.3mf')}`;
  },
  uploadSource3mf: async (archiveId: number, file: File): Promise<{ status: string; filename: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${archiveId}/source`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  deleteSource3mf: (archiveId: number) =>
    request<{ status: string }>(`/archives/${archiveId}/source`, {
      method: 'DELETE',
    }),
  // F3D (Fusion 360 design file)
  getF3dDownloadUrl: (archiveId: number) =>
    `${API_BASE}/archives/${archiveId}/f3d`,
  downloadF3d: async (archiveId: number): Promise<void> => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${archiveId}/f3d`, { headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get('Content-Disposition');
    const filename = parseContentDispositionFilename(disposition) || `archive_${archiveId}.f3d`;
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },
  uploadF3d: async (archiveId: number, file: File): Promise<{ status: string; filename: string }> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/archives/${archiveId}/f3d`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  deleteF3d: (archiveId: number) =>
    request<{ status: string }>(`/archives/${archiveId}/f3d`, {
      method: 'DELETE',
    }),

  // QR Code
  getArchiveQRCodeUrl: (archiveId: number, size = 200) =>
    `${API_BASE}/archives/${archiveId}/qrcode?size=${size}`,
  getArchiveCapabilities: (id: number) =>
    request<{
      has_model: boolean;
      has_gcode: boolean;
      has_source: boolean;
      build_volume: { x: number; y: number; z: number };
      filament_colors: string[];
    }>(`/archives/${id}/capabilities`),
  getLibraryFileCapabilities: (id: number) =>
    request<{
      has_model: boolean;
      has_gcode: boolean;
      has_source: boolean;
      build_volume: { x: number; y: number; z: number };
      filament_colors: string[];
    }>(`/library/files/${id}/capabilities`),
  // Project Page
  getArchiveProjectPage: (id: number) =>
    request<{
      title: string | null;
      description: string | null;
      designer: string | null;
      designer_user_id: string | null;
      license: string | null;
      copyright: string | null;
      creation_date: string | null;
      modification_date: string | null;
      origin: string | null;
      profile_title: string | null;
      profile_description: string | null;
      profile_cover: string | null;
      profile_user_id: string | null;
      profile_user_name: string | null;
      design_model_id: string | null;
      design_profile_id: string | null;
      design_region: string | null;
      model_pictures: Array<{ name: string; path: string; url: string }>;
      profile_pictures: Array<{ name: string; path: string; url: string }>;
      thumbnails: Array<{ name: string; path: string; url: string }>;
    }>(`/archives/${id}/project-page`),
  updateArchiveProjectPage: (id: number, data: {
    title?: string;
    description?: string;
    designer?: string;
    license?: string;
    copyright?: string;
    profile_title?: string;
    profile_description?: string;
  }) =>
    request(`/archives/${id}/project-page`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  getArchiveProjectImageUrl: (archiveId: number, imagePath: string) =>
    `${API_BASE}/archives/${archiveId}/project-image/${encodeURIComponent(imagePath)}`,
  getArchiveForSlicer: (id: number, filename: string) => {
    const safe = filename.replace(/[/\\?#]/g, '_');
    return `${API_BASE}/archives/${id}/file/${encodeURIComponent(safe.endsWith('.3mf') ? safe : safe + '.3mf')}`;
  },
  createArchiveSlicerToken: (archiveId: number) =>
    request<{ token: string }>(`/archives/${archiveId}/slicer-token`, { method: 'POST' }),
  getArchiveSlicerDownloadUrl: (archiveId: number, token: string, filename: string) => {
    const safe = filename.replace(/[/\\?#]/g, '_');
    return `${API_BASE}/archives/${archiveId}/dl/${token}/${encodeURIComponent(safe.endsWith('.3mf') ? safe : safe + '.3mf')}`;
  },
  getArchivePlates: (archiveId: number) =>
    request<ArchivePlatesResponse>(`/archives/${archiveId}/plates`),
  getArchiveFilamentRequirements: (archiveId: number, plateId?: number, requestId?: string) => {
    // request_id flows to the sidecar's preview-slice fallback so the
    // SliceModal's inline spinner can poll matching live progress.
    const params = new URLSearchParams();
    if (plateId !== undefined) params.set('plate_id', String(plateId));
    if (requestId !== undefined) params.set('request_id', requestId);
    const qs = params.toString();
    return request<{
      archive_id: number;
      filename: string;
      plate_id: number | null;
      filaments: Array<{
        slot_id: number;
        type: string;
        color: string;
        used_grams: number;
        used_meters: number;
        used_in_plate?: boolean;
      }>;
    }>(`/archives/${archiveId}/filament-requirements${qs ? `?${qs}` : ''}`);
  },
  retryArchiveDownload: (archiveId: number) =>
    request<{
      status: 'recovered' | 'already_has_file' | 'in_progress' | 'failed' | 'error';
      recovered: boolean;
      message: string;
    }>(`/archives/${archiveId}/retry-download`, { method: 'POST' }),
  reprintArchive: (
    archiveId: number,
    printerId: number,
    options?: {
      plate_id?: number;
      plate_name?: string;
      ams_mapping?: number[];
      timelapse?: boolean;
      bed_levelling?: boolean;
      flow_cali?: boolean;
      layer_inspect?: boolean;
      use_ams?: boolean;
      mesh_mode_fast_check?: boolean;
      execute_swap_macros?: boolean;
      swap_macro_events?: string[] | null;
      quantity?: number;
    }
  ) =>
    request<BackgroundDispatchResponse>(
      `/archives/${archiveId}/reprint?printer_id=${printerId}`,
      {
        method: 'POST',
        headers: options ? { 'Content-Type': 'application/json' } : undefined,
        body: options ? JSON.stringify(options) : undefined,
      }
    ),
  // Settings
  getSettings: () => request<AppSettings>('/settings/'),
  getDefaultSidebarOrder: () => request<{ default_sidebar_order: string }>('/settings/default-sidebar-order'),
  updateSettings: (data: AppSettingsUpdate) =>
    request<AppSettings>('/settings/', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  getMQTTStatus: () => request<MQTTStatus>('/settings/mqtt/status'),
  resetSettings: () =>
    request<AppSettings>('/settings/reset', { method: 'POST' }),
  exportBackup: async (): Promise<{ blob: Blob; filename: string }> => {
    // New simplified backup - complete database + all files
    const url = `${API_BASE}/settings/backup`;
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(url, { headers });

    // Check for errors
    if (!response.ok) {
      const errorText = await response.text();
      throw new Error(errorText || `Backup failed with status ${response.status}`);
    }

    // Get filename from Content-Disposition header
    const contentDisposition = response.headers.get('Content-Disposition');
    let filename = 'bamdude-backup.zip';
    if (contentDisposition) {
      const match = contentDisposition.match(/filename=([^;]+)/);
      if (match) filename = match[1].trim();
    }

    const blob = await response.blob();
    return { blob, filename };
  },
  importBackup: async (file: File) => {
    // New simplified restore - replaces database + all directories
    const formData = new FormData();
    formData.append('file', file);
    const url = `${API_BASE}/settings/restore`;
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(url, {
      method: 'POST',
      headers,
      body: formData,
    });
    return response.json() as Promise<{
      success: boolean;
      message: string;
    }>;
  },
  optimizeDatabase: () =>
    request<{ success: boolean; message: string; db_size: number; wal_size: number }>('/settings/optimize-db', { method: 'POST' }),
  checkFfmpeg: () =>
    request<{ installed: boolean; path: string | null }>('/settings/check-ffmpeg'),
  getNetworkInterfaces: () =>
    request<{ interfaces: NetworkInterface[] }>('/settings/network-interfaces'),

  // Cloud
  getCloudStatus: () => request<CloudAuthStatus>('/cloud/status'),
  cloudLogin: (email: string, password: string, region = 'global') =>
    request<CloudLoginResponse>('/cloud/login', {
      method: 'POST',
      body: JSON.stringify({ email, password, region }),
    }),
  cloudVerify: (email: string, code: string, tfaKey?: string, region: string = 'global') =>
    request<CloudLoginResponse>('/cloud/verify', {
      method: 'POST',
      body: JSON.stringify({ email, code, tfa_key: tfaKey, region }),
    }),
  cloudSetToken: (access_token: string, region: string = 'global') =>
    request<CloudAuthStatus>('/cloud/token', {
      method: 'POST',
      body: JSON.stringify({ access_token, region }),
    }),
  cloudLogout: () =>
    request<{ success: boolean }>('/cloud/logout', { method: 'POST' }),
  getCloudSettings: (version = '02.04.00.70') =>
    request<SlicerSettingsResponse>(`/cloud/settings?version=${version}`),
  getBuiltinFilaments: () =>
    request<BuiltinFilament[]>('/cloud/builtin-filaments'),
  getFilamentIdMap: () =>
    request<Record<string, string>>('/cloud/filament-id-map'),

  // MakerWorld URL-paste import flow (B.5 — Phase 5/6 of 0.5.x cycle).
  getMakerworldStatus: () =>
    request<MakerworldStatus>('/makerworld/status'),
  resolveMakerworldUrl: (url: string) =>
    request<MakerworldResolvedModel>('/makerworld/resolve', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),
  getMakerworldRecentImports: (limit = 10) =>
    request<MakerworldRecentImport[]>(`/makerworld/recent-imports?limit=${limit}`),
  getMakerworldImports: (params: MakerworldImportsListParams = {}) => {
    const qs = new URLSearchParams();
    if (params.page) qs.set('page', String(params.page));
    if (params.per_page) qs.set('per_page', String(params.per_page));
    if (params.search) qs.set('search', params.search);
    if (params.sort_by) qs.set('sort_by', params.sort_by);
    return request<MakerworldImportsPage>(`/makerworld/imports?${qs}`);
  },
  getMakerworldImportCoverUrl: (libraryFileId: number, variant = false) =>
    `/api/v1/makerworld/imports/${libraryFileId}/${variant ? 'cover-variant' : 'cover'}`,
  importMakerworldInstance: (
    model_id: number,
    instance_id: number | null,
    profile_id?: number | null,
    folder_id?: number | null,
  ) =>
    request<MakerworldImportResponse>('/makerworld/import', {
      method: 'POST',
      body: JSON.stringify({
        model_id,
        instance_id: instance_id ?? null,
        profile_id: profile_id ?? null,
        folder_id: folder_id ?? null,
      }),
    }),
  redownloadMakerworldImport: (libraryFileId: number) =>
    request<MakerworldImportResponse>(`/makerworld/imports/${libraryFileId}/redownload`, {
      method: 'POST',
    }),
  getCloudSettingDetail: (settingId: string) =>
    request<SlicerSettingDetail>(`/cloud/settings/${settingId}`),
  createCloudSetting: (data: SlicerSettingCreate) =>
    request<SlicerSettingDetail>('/cloud/settings', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateCloudSetting: (settingId: string, data: SlicerSettingUpdate) =>
    request<SlicerSettingDetail>(`/cloud/settings/${settingId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteCloudSetting: (settingId: string) =>
    request<SlicerSettingDeleteResponse>(`/cloud/settings/${settingId}`, {
      method: 'DELETE',
    }),
  getCloudDevices: () => request<CloudDevice[]>('/cloud/devices'),
  getCloudFields: (presetType: 'filament' | 'print' | 'process' | 'printer') =>
    request<FieldDefinitionsResponse>(`/cloud/fields/${presetType}`),
  getAllCloudFields: () =>
    request<Record<string, FieldDefinitionsResponse>>('/cloud/fields'),
  getFilamentInfo: (settingIds: string[]) =>
    request<
      Record<
        string,
        {
          name: string;
          k: number | null;
          // Optional fields populated for cloud-resolved presets — used by
          // the calibration wizard to auto-fill bed / nozzle / max-vol-
          // speed from the operator's picked filament preset instead of
          // making them type the values manually.
          nozzle_temperature?: number;
          hot_plate_temp?: number;
          filament_max_volumetric_speed?: number;
        }
      >
    >('/cloud/filament-info', {
      method: 'POST',
      body: JSON.stringify(settingIds),
    }),

  // Smart Plugs
  getSmartPlugs: () => request<SmartPlug[]>('/smart-plugs/'),
  getSmartPlug: (id: number) => request<SmartPlug>(`/smart-plugs/${id}`),
  getSmartPlugByPrinter: (printerId: number) => request<SmartPlug | null>(`/smart-plugs/by-printer/${printerId}`),
  getScriptPlugsByPrinter: (printerId: number) => request<SmartPlug[]>(`/smart-plugs/by-printer/${printerId}/scripts`),
  createSmartPlug: (data: SmartPlugCreate) =>
    request<SmartPlug>('/smart-plugs/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateSmartPlug: (id: number, data: SmartPlugUpdate) =>
    request<SmartPlug>(`/smart-plugs/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteSmartPlug: (id: number) =>
    request<void>(`/smart-plugs/${id}`, { method: 'DELETE' }),
  controlSmartPlug: (id: number, action: 'on' | 'off' | 'toggle') =>
    request<{ success: boolean; action: string }>(`/smart-plugs/${id}/control`, {
      method: 'POST',
      body: JSON.stringify({ action }),
    }),
  getSmartPlugStatus: (id: number) =>
    request<SmartPlugStatus>(`/smart-plugs/${id}/status`),
  testSmartPlugConnection: (ip_address: string, username?: string | null, password?: string | null) =>
    request<SmartPlugTestResult>('/smart-plugs/test-connection', {
      method: 'POST',
      body: JSON.stringify({ ip_address, username, password }),
    }),

  // Tasmota Discovery (auto-detects network)
  startTasmotaScan: () =>
    request<TasmotaScanStatus>('/smart-plugs/discover/scan', { method: 'POST' }),
  getTasmotaScanStatus: () =>
    request<TasmotaScanStatus>('/smart-plugs/discover/status'),
  stopTasmotaScan: () =>
    request<TasmotaScanStatus>('/smart-plugs/discover/stop', { method: 'POST' }),
  getDiscoveredTasmotaDevices: () =>
    request<DiscoveredTasmotaDevice[]>('/smart-plugs/discover/devices'),

  // Home Assistant Integration
  testHAConnection: (url: string, token: string) =>
    request<HATestConnectionResult>('/smart-plugs/ha/test-connection', {
      method: 'POST',
      body: JSON.stringify({ url, token }),
    }),
  getHAEntities: (search?: string) => {
    const params = search ? `?search=${encodeURIComponent(search)}` : '';
    return request<HAEntity[]>(`/smart-plugs/ha/entities${params}`);
  },
  getHASensorEntities: () =>
    request<HASensorEntity[]>('/smart-plugs/ha/sensors'),

  // REST smart plug
  testRESTConnection: (url: string, method: string = 'GET', headers?: string | null) =>
    request<{ success: boolean; error: string | null }>('/smart-plugs/rest/test-connection', {
      method: 'POST',
      body: JSON.stringify({ url, method, headers }),
    }),

  // Print Queue
  getQueue: (queueId?: number, status?: string) => {
    const params = new URLSearchParams();
    if (queueId) params.set('queue_id', String(queueId));
    if (status) params.set('status', status);
    return request<PrintQueueItem[]>(`/queue/?${params}`);
  },
  getQueueItem: (id: number) => request<PrintQueueItem>(`/queue/${id}`),
  addToQueue: (data: PrintQueueItemCreate) =>
    request<PrintQueueItem>('/queue/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateQueueItem: (id: number, data: PrintQueueItemUpdate) =>
    request<PrintQueueItem>(`/queue/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  removeFromQueue: (id: number) =>
    request<{ message: string }>(`/queue/${id}`, { method: 'DELETE' }),
  getStaggerState: () => request<StaggerState>('/queue/stagger-state'),
  // Queue item commands
  reorderQueueItem: (id: number, direction: 'up' | 'down') =>
    request<{ moved: number; direction: string; block_size: number }>(
      `/queue/${id}/reorder?direction=${direction}`,
      { method: 'POST' }
    ),
  bumpQueueItem: (id: number) =>
    request<{ shifted: number; block_size: number }>(`/queue/${id}/bump`, { method: 'POST' }),
  bumpQueueItemBottom: (id: number) =>
    request<{ shifted: number; block_size: number }>(`/queue/${id}/bump-bottom`, { method: 'POST' }),
  cloneQueueItem: (id: number, scope: 'single' | 'batch' = 'single') =>
    request<PrintQueueItem>(`/queue/${id}/clone?scope=${scope}`, { method: 'POST' }),
  skipQueueItem: (id: number) =>
    request<{ status: string; item_id: number }>(`/queue/${id}/skip`, { method: 'POST' }),
  unskipQueueItem: (id: number) =>
    request<{ status: string; item_id: number }>(`/queue/${id}/unskip`, { method: 'POST' }),
  toggleManualStart: (id: number) =>
    request<{ manual_start: boolean; item_id: number }>(`/queue/${id}/manual-start`, { method: 'PATCH' }),
  retryQueueItem: (id: number) =>
    request<PrintQueueItem>(`/queue/${id}/retry`, { method: 'POST' }),
  // Batch operations
  cancelBatch: (batchId: string) =>
    request<{ cancelled: number; batch_id: string }>(`/queue/batch/${batchId}/cancel`, { method: 'POST' }),
  skipBatch: (batchId: string) =>
    request<{ skipped: number; batch_id: string }>(`/queue/batch/${batchId}/skip`, { method: 'POST' }),
  reorderBatch: (batchId: string, direction: 'up' | 'down') =>
    request<{ moved: number; direction: string; batch_size: number }>(
      `/queue/batch/${batchId}/reorder?direction=${direction}`,
      { method: 'POST' }
    ),
  bumpBatch: (batchId: string) =>
    request<{ shifted: number; batch_size: number }>(`/queue/batch/${batchId}/bump`, { method: 'POST' }),
  bumpBatchBottom: (batchId: string) =>
    request<{ shifted: number; batch_size: number }>(`/queue/batch/${batchId}/bump-bottom`, { method: 'POST' }),
  updateBatch: (batchId: string, data: PrintQueueItemUpdate) =>
    request<{ updated: number; batch_id: string; fields: string[] }>(`/queue/batch/${batchId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  cloneBatch: (batchId: string, scope: 'one' | 'batch' = 'batch') =>
    request<{ cloned: number; scope: string; source_batch_id?: string; new_batch_id?: string; new_item_id?: number }>(
      `/queue/batch/${batchId}/clone?scope=${scope}`,
      { method: 'POST' }
    ),
  reorderQueue: (items: { id: number; position: number }[]) =>
    request<{ message: string }>('/queue/reorder', {
      method: 'POST',
      body: JSON.stringify({ items }),
    }),
  cancelQueueItem: (id: number) =>
    request<{ message: string }>(`/queue/${id}/cancel`, { method: 'POST' }),
  stopQueueItem: (id: number) =>
    request<{ message: string }>(`/queue/${id}/stop`, { method: 'POST' }),
  startQueueItem: (id: number) =>
    request<PrintQueueItem>(`/queue/${id}/start`, { method: 'POST' }),
  bulkUpdateQueue: (data: PrintQueueBulkUpdate) =>
    request<PrintQueueBulkUpdateResponse>('/queue/bulk', {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  // Auto Queue — single global router-queue above per-printer queues
  getAutoQueue: (status?: 'pending' | 'assigned' | 'cancelled', batchId?: string) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    if (batchId) params.set('batch_id', batchId);
    const qs = params.toString();
    return request<AutoQueueItem[]>(`/auto-queue/${qs ? `?${qs}` : ''}`);
  },
  getAutoQueueStats: () => request<AutoQueueStats>('/auto-queue/stats'),
  getAutoQueueItem: (id: number) => request<AutoQueueItem>(`/auto-queue/${id}`),
  addToAutoQueue: (data: AutoQueueItemCreate) =>
    request<AutoQueueItem>('/auto-queue/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateAutoQueueItem: (id: number, data: AutoQueueItemUpdate) =>
    request<AutoQueueItem>(`/auto-queue/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  removeFromAutoQueue: (id: number) =>
    request<AutoQueueItem>(`/auto-queue/${id}`, { method: 'DELETE' }),
  reorderAutoQueue: (items: { id: number; position: number }[]) =>
    request<{ reordered: number }>('/auto-queue/reorder', {
      method: 'POST',
      body: JSON.stringify({ items }),
    }),
  assignAutoQueueNow: (id: number) =>
    request<AutoQueueItem>(`/auto-queue/${id}/assign-now`, { method: 'POST' }),
  cancelAutoQueueBatch: (batchId: string) =>
    request<{ affected: number; batch_id: string }>(`/auto-queue/batch/${batchId}`, {
      method: 'DELETE',
    }),

  // Printer Queues (queue-level operations)
  getQueues: () =>
    request<PrinterQueue[]>('/queues/'),
  updateQueue: (queueId: number, data: { status?: 'idle' | 'paused'; is_paused?: boolean }) =>
    request<PrinterQueue>(`/queues/${queueId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),

  // K-Profiles
  getKProfiles: (printerId: number, nozzleDiameter = '0.4') =>
    request<KProfilesResponse>(`/printers/${printerId}/kprofiles/?nozzle_diameter=${nozzleDiameter}`),
  setKProfile: (printerId: number, profile: KProfileCreate) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/kprofiles/`, {
      method: 'POST',
      body: JSON.stringify(profile),
    }),
  deleteKProfile: (printerId: number, profile: KProfileDelete) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/kprofiles/`, {
      method: 'DELETE',
      body: JSON.stringify(profile),
    }),
  setKProfilesBatch: (printerId: number, profiles: KProfileCreate[]) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/kprofiles/batch`, {
      method: 'POST',
      body: JSON.stringify(profiles),
    }),

  // K-Profile Notes — keyed by stable filament_calibration_id since m065.
  // `settingId` is accepted as a hint when caller doesn't have the fc_id;
  // backend resolves via the printer's live K-profile list.
  getKProfileNotes: (printerId: number) =>
    request<KProfileNotesResponse>(`/printers/${printerId}/kprofiles/notes`),
  setKProfileNote: (
    printerId: number,
    payload: { filament_calibration_id?: number; setting_id?: string; note: string },
  ) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/kprofiles/notes`, {
      method: 'PUT',
      body: JSON.stringify(payload),
    }),
  deleteKProfileNote: (printerId: number, filamentCalibrationId: number) =>
    request<{ success: boolean; message: string }>(`/printers/${printerId}/kprofiles/notes/${filamentCalibrationId}`, {
      method: 'DELETE',
    }),

  // Slot Preset Mappings
  getSlotPresets: (printerId: number) =>
    request<Record<number, SlotPresetMapping>>(`/printers/${printerId}/slot-presets`),
  getSlotPreset: (printerId: number, amsId: number, trayId: number) =>
    request<SlotPresetMapping | null>(`/printers/${printerId}/slot-presets/${amsId}/${trayId}`),
  saveSlotPreset: (printerId: number, amsId: number, trayId: number, presetId: string, presetName: string, presetSource = 'cloud') =>
    request<SlotPresetMapping>(`/printers/${printerId}/slot-presets/${amsId}/${trayId}?preset_id=${encodeURIComponent(presetId)}&preset_name=${encodeURIComponent(presetName)}&preset_source=${encodeURIComponent(presetSource)}`, {
      method: 'PUT',
    }),
  deleteSlotPreset: (printerId: number, amsId: number, trayId: number) =>
    request<{ success: boolean }>(`/printers/${printerId}/slot-presets/${amsId}/${trayId}`, {
      method: 'DELETE',
    }),

  // AMS Labels (user-defined friendly names)
  getAmsLabels: (printerId: number) =>
    request<Record<number, string>>(`/printers/${printerId}/ams-labels`),
  saveAmsLabel: (printerId: number, amsId: number, label: string, amsSerial = '') =>
    request<{ ams_id: number; label: string }>(
      `/printers/${printerId}/ams-labels/${amsId}`,
      {
        method: 'PUT',
        body: JSON.stringify({ label, ams_serial: amsSerial }),
      }
    ),
  deleteAmsLabel: (printerId: number, amsId: number, amsSerial = '') =>
    request<{ success: boolean }>(`/printers/${printerId}/ams-labels/${amsId}?ams_serial=${encodeURIComponent(amsSerial)}`, {
      method: 'DELETE',
    }),

  configureAmsSlot: (
    printerId: number,
    amsId: number,
    trayId: number,
    config: {
      tray_info_idx: string;
      tray_type: string;
      tray_sub_brands: string;
      tray_color: string;
      nozzle_temp_min: number;
      nozzle_temp_max: number;
      cali_idx: number;
      nozzle_diameter: string;
      setting_id?: string;
      kprofile_filament_id?: string;
      kprofile_setting_id?: string;
      k_value?: number;
    }
  ) => {
    const params = new URLSearchParams({
      tray_info_idx: config.tray_info_idx,
      tray_type: config.tray_type,
      tray_sub_brands: config.tray_sub_brands,
      tray_color: config.tray_color,
      nozzle_temp_min: config.nozzle_temp_min.toString(),
      nozzle_temp_max: config.nozzle_temp_max.toString(),
      cali_idx: config.cali_idx.toString(),
      nozzle_diameter: config.nozzle_diameter,
    });
    if (config.setting_id) {
      params.set('setting_id', config.setting_id);
    }
    if (config.kprofile_filament_id) {
      params.set('kprofile_filament_id', config.kprofile_filament_id);
    }
    if (config.kprofile_setting_id) {
      params.set('kprofile_setting_id', config.kprofile_setting_id);
    }
    if (config.k_value !== undefined && config.k_value > 0) {
      params.set('k_value', config.k_value.toString());
    }
    return request<{ success: boolean; message: string }>(
      `/printers/${printerId}/slots/${amsId}/${trayId}/configure?${params}`,
      { method: 'POST' }
    );
  },
  resetAmsSlot: (printerId: number, amsId: number, trayId: number) =>
    request<{ success: boolean; message: string }>(
      `/printers/${printerId}/ams/${amsId}/tray/${trayId}/reset`,
      { method: 'POST' }
    ),


  // Notification Providers
  getNotificationProviders: () => request<NotificationProvider[]>('/notifications/'),
  getNotificationProvider: (id: number) => request<NotificationProvider>(`/notifications/${id}`),
  createNotificationProvider: (data: NotificationProviderCreate) =>
    request<NotificationProvider>('/notifications/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateNotificationProvider: (id: number, data: NotificationProviderUpdate) =>
    request<NotificationProvider>(`/notifications/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteNotificationProvider: (id: number) =>
    request<{ message: string }>(`/notifications/${id}`, { method: 'DELETE' }),
  testNotificationProvider: (id: number) =>
    request<NotificationTestResponse>(`/notifications/${id}/test`, { method: 'POST' }),
  testNotificationConfig: (data: NotificationTestRequest) =>
    request<NotificationTestResponse>('/notifications/test-config', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  testAllNotificationProviders: () =>
    request<{
      tested: number;
      success: number;
      failed: number;
      results: Array<{
        provider_id: number;
        provider_name: string;
        provider_type: string;
        success: boolean;
        message: string;
      }>;
    }>('/notifications/test-all', { method: 'POST' }),

  // Notification Templates
  getNotificationTemplates: () => request<NotificationTemplate[]>('/notification-templates'),
  getNotificationTemplate: (id: number) => request<NotificationTemplate>(`/notification-templates/${id}`),
  updateNotificationTemplate: (id: number, data: NotificationTemplateUpdate) =>
    request<NotificationTemplate>(`/notification-templates/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  resetNotificationTemplate: (id: number) =>
    request<NotificationTemplate>(`/notification-templates/${id}/reset`, {
      method: 'POST',
    }),
  getTemplateVariables: () => request<EventVariablesResponse[]>('/notification-templates/variables'),
  previewTemplate: (data: TemplatePreviewRequest) =>
    request<TemplatePreviewResponse>('/notification-templates/preview', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  // Notification Logs
  getNotificationLogs: (params?: {
    limit?: number;
    offset?: number;
    provider_id?: number;
    event_type?: string;
    success?: boolean;
    days?: number;
  }) => {
    const searchParams = new URLSearchParams();
    if (params?.limit) searchParams.set('limit', String(params.limit));
    if (params?.offset) searchParams.set('offset', String(params.offset));
    if (params?.provider_id) searchParams.set('provider_id', String(params.provider_id));
    if (params?.event_type) searchParams.set('event_type', params.event_type);
    if (params?.success !== undefined) searchParams.set('success', String(params.success));
    if (params?.days) searchParams.set('days', String(params.days));
    return request<NotificationLogEntry[]>(`/notifications/logs?${searchParams}`);
  },
  getNotificationLogStats: (days = 7) =>
    request<NotificationLogStats>(`/notifications/logs/stats?days=${days}`),
  clearNotificationLogs: (olderThanDays = 30) =>
    request<{ deleted: number; message: string }>(
      `/notifications/logs?older_than_days=${olderThanDays}`,
      { method: 'DELETE' }
    ),

  // Spoolman Integration
  getSpoolmanStatus: () => request<SpoolmanStatus>('/spoolman/status'),
  connectSpoolman: () =>
    request<{ success: boolean; message: string }>('/spoolman/connect', {
      method: 'POST',
    }),
  disconnectSpoolman: () =>
    request<{ success: boolean; message: string }>('/spoolman/disconnect', {
      method: 'POST',
    }),
  syncPrinterAms: (printerId: number) =>
    request<SpoolmanSyncResult>(`/spoolman/sync/${printerId}`, {
      method: 'POST',
    }),
  syncAllPrintersAms: () =>
    request<SpoolmanSyncResult>('/spoolman/sync-all', {
      method: 'POST',
    }),
  getSpoolmanSpools: () =>
    request<{ spools: unknown[] }>('/spoolman/spools'),
  getSpoolmanFilaments: () =>
    request<{ filaments: unknown[] }>('/spoolman/filaments'),
  getUnlinkedSpools: () =>
    request<UnlinkedSpool[]>('/spoolman/spools/unlinked'),
  getLinkedSpools: () =>
    request<LinkedSpoolsMap>('/spoolman/spools/linked'),
  linkSpool: (
    spoolId: number,
    context: {
      spoolTag: string;
      printerId: number;
      amsId: number;
      trayId: number;
    }
  ) =>
    request<{ success: boolean; message: string }>(`/spoolman/spools/${spoolId}/link`, {
      method: 'POST',
      body: JSON.stringify({
        spool_tag: context.spoolTag,
        printer_id: context.printerId,
        ams_id: context.amsId,
        tray_id: context.trayId,
      }),
    }),
  unlinkSpool: (spoolId: number) =>
    request<{ success: boolean; message: string }>(`/spoolman/spools/${spoolId}/unlink`, {
      method: 'POST',
    }),
  getSpoolmanSettings: () =>
    request<{ spoolman_enabled: string; spoolman_url: string; spoolman_sync_mode: string; spoolman_disable_weight_sync: string; spoolman_report_partial_usage: string; }>('/settings/spoolman'),
  updateSpoolmanSettings: (data: { spoolman_enabled?: string; spoolman_url?: string; spoolman_sync_mode?: string; spoolman_disable_weight_sync?: string; spoolman_report_partial_usage?: string; }) =>
    request<{ spoolman_enabled: string; spoolman_url: string; spoolman_sync_mode: string; spoolman_disable_weight_sync: string; spoolman_report_partial_usage: string; }>('/settings/spoolman', {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  // Spool labels (B.1) — PDF rendered server-side, returned as Blob.
  // ``display_name`` is the per-spool override for the label's bold central
  // line; the modal forwards what ``formatSpoolDisplayName`` produced from
  // the user's spool_display_template setting so the label matches the UI.
  printSpoolLabels: async (body: SpoolLabelRequest): Promise<Blob> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const response = await fetch(`${API_BASE}/inventory/labels`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.blob();
  },
  printSpoolmanSpoolLabels: async (body: SpoolLabelRequest): Promise<Blob> => {
    const headers: Record<string, string> = { 'Content-Type': 'application/json' };
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const response = await fetch(`${API_BASE}/spoolman/labels`, {
      method: 'POST',
      headers,
      body: JSON.stringify(body),
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.blob();
  },

  // Inventory
  getSpools: (includeArchived = false) =>
    request<InventorySpool[]>(`/inventory/spools?include_archived=${includeArchived}`),
  getSpool: (id: number) => request<InventorySpool>(`/inventory/spools/${id}`),
  createSpool: (data: Omit<InventorySpool, 'id' | 'archived_at' | 'created_at' | 'updated_at' | 'k_profiles'>) =>
    request<InventorySpool>('/inventory/spools', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  bulkCreateSpools: (
    data: Omit<InventorySpool, 'id' | 'archived_at' | 'created_at' | 'updated_at' | 'k_profiles'>,
    quantity: number,
    autoIncrementLot = false,
  ) =>
    request<InventorySpool[]>('/inventory/spools/bulk', {
      method: 'POST',
      body: JSON.stringify({ spool: data, quantity, auto_increment_lot: autoIncrementLot }),
    }),
  updateSpool: (id: number, data: Partial<Omit<InventorySpool, 'id' | 'archived_at' | 'created_at' | 'updated_at' | 'k_profiles'>>) =>
    request<InventorySpool>(`/inventory/spools/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteSpool: (id: number) =>
    request<{ status: string }>(`/inventory/spools/${id}`, { method: 'DELETE' }),
  archiveSpool: (id: number) =>
    request<InventorySpool>(`/inventory/spools/${id}/archive`, { method: 'POST' }),
  restoreSpool: (id: number) =>
    request<InventorySpool>(`/inventory/spools/${id}/restore`, { method: 'POST' }),
  getSpoolKProfiles: (spoolId: number) =>
    request<SpoolKProfile[]>(`/inventory/spools/${spoolId}/k-profiles`),
  saveSpoolKProfiles: (spoolId: number, profiles: SpoolKProfileInput[]) =>
    request<SpoolKProfile[]>(`/inventory/spools/${spoolId}/k-profiles`, {
      method: 'PUT',
      body: JSON.stringify(profiles),
    }),

  // Spoolman Inventory proxy (unified UI when Spoolman is enabled — port of upstream PR #1241).
  getSpoolmanInventoryFilaments: () =>
    request<SpoolmanFilamentEntry[]>('/spoolman/inventory/filaments'),
  patchSpoolmanFilament: (filamentId: number, data: SpoolmanFilamentPatch) =>
    request<SpoolmanFilamentEntry>(`/spoolman/inventory/filaments/${filamentId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  getSpoolmanInventorySpools: (includeArchived = false) =>
    request<InventorySpool[]>(`/spoolman/inventory/spools?include_archived=${includeArchived}`),
  getSpoolmanInventorySpool: (id: number) =>
    request<InventorySpool>(`/spoolman/inventory/spools/${id}`),
  createSpoolmanInventorySpool: (
    data: Omit<InventorySpool, 'id' | 'archived_at' | 'created_at' | 'updated_at' | 'k_profiles'>,
  ) =>
    request<InventorySpool>('/spoolman/inventory/spools', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  bulkCreateSpoolmanInventorySpools: (
    data: Omit<InventorySpool, 'id' | 'archived_at' | 'created_at' | 'updated_at' | 'k_profiles'>,
    quantity: number,
  ) =>
    request<SpoolmanBulkCreateResult | InventorySpool[]>(
      '/spoolman/inventory/spools/bulk',
      {
        method: 'POST',
        body: JSON.stringify({ spool: data, quantity }),
      },
    ),
  updateSpoolmanInventorySpool: (
    id: number,
    data: Partial<Omit<InventorySpool, 'id' | 'archived_at' | 'created_at' | 'updated_at' | 'k_profiles'>>,
  ) =>
    request<InventorySpool>(`/spoolman/inventory/spools/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteSpoolmanInventorySpool: (id: number) =>
    request<{ status: string }>(`/spoolman/inventory/spools/${id}`, { method: 'DELETE' }),
  archiveSpoolmanInventorySpool: (id: number) =>
    request<InventorySpool>(`/spoolman/inventory/spools/${id}/archive`, { method: 'POST' }),
  restoreSpoolmanInventorySpool: (id: number) =>
    request<InventorySpool>(`/spoolman/inventory/spools/${id}/restore`, { method: 'POST' }),
  linkTagToSpoolmanSpool: (
    spoolId: number,
    data: { tag_uid?: string; tray_uuid?: string },
  ) =>
    request<InventorySpool>(`/spoolman/inventory/spools/${spoolId}/tag`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  syncSpoolmanSpoolWeight: (spoolId: number, weightGrams: number) =>
    request<{ status: string; weight_used: number }>(
      `/spoolman/inventory/spools/${spoolId}/weight`,
      {
        method: 'PATCH',
        body: JSON.stringify({ weight_grams: weightGrams }),
      },
    ),
  assignSpoolmanSlot: (data: {
    spoolman_spool_id: number;
    printer_id: number;
    ams_id: number;
    tray_id: number;
  }) =>
    request<InventorySpool>('/spoolman/inventory/slot-assignments', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  unassignSpoolmanSlot: (spoolmanSpoolId: number) =>
    request<InventorySpool>(`/spoolman/inventory/slot-assignments/${spoolmanSpoolId}`, {
      method: 'DELETE',
    }),
  getSpoolmanSlotAssignment: (printerId: number, amsId: number, trayId: number) =>
    request<InventorySpool | null>(
      `/spoolman/inventory/slot-assignments?printer_id=${printerId}&ams_id=${amsId}&tray_id=${trayId}`,
    ),
  getSpoolmanSlotAssignments: (printerId?: number) =>
    request<SpoolmanSlotAssignmentEnriched[]>(
      printerId !== undefined
        ? `/spoolman/inventory/slot-assignments/all?printer_id=${printerId}`
        : '/spoolman/inventory/slot-assignments/all',
    ),
  syncSpoolmanAmsWeights: () =>
    request<{ synced: number; skipped: number }>('/spoolman/inventory/sync-ams-weights', {
      method: 'POST',
    }),
  getSpoolmanKProfiles: (spoolId: number) =>
    request<SpoolKProfile[]>(`/spoolman/inventory/spools/${spoolId}/k-profiles`),
  saveSpoolmanKProfiles: (spoolId: number, profiles: SpoolKProfileInput[]) =>
    request<SpoolKProfile[]>(`/spoolman/inventory/spools/${spoolId}/k-profiles`, {
      method: 'PUT',
      body: JSON.stringify(profiles),
    }),

  getAssignments: (printerId?: number) =>
    request<SpoolAssignment[]>(`/inventory/assignments${printerId ? `?printer_id=${printerId}` : ''}`),
  assignSpool: (data: { spool_id: number; printer_id: number; ams_id: number; tray_id: number }) =>
    request<SpoolAssignment>('/inventory/assignments', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  unassignSpool: (printerId: number, amsId: number, trayId: number) =>
    request<{ status: string }>(`/inventory/assignments/${printerId}/${amsId}/${trayId}`, { method: 'DELETE' }),
  getSpoolCatalog: () =>
    request<SpoolCatalogEntry[]>('/inventory/catalog'),
  addCatalogEntry: (data: { name: string; weight: number }) =>
    request<SpoolCatalogEntry>('/inventory/catalog', { method: 'POST', body: JSON.stringify(data) }),
  updateCatalogEntry: (id: number, data: { name: string; weight: number }) =>
    request<SpoolCatalogEntry>(`/inventory/catalog/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteCatalogEntry: (id: number) =>
    request<{ status: string }>(`/inventory/catalog/${id}`, { method: 'DELETE' }),
  bulkDeleteCatalogEntries: (ids: number[]) =>
    request<{ deleted: number }>('/inventory/catalog/bulk-delete', { method: 'POST', body: JSON.stringify({ ids }) }),
  resetSpoolCatalog: () =>
    request<{ status: string }>('/inventory/catalog/reset', { method: 'POST' }),
  getColorCatalog: () =>
    request<ColorCatalogEntry[]>('/inventory/colors'),
  getColorNameMap: () =>
    request<{ colors: Record<string, string> }>('/inventory/colors/map'),
  addColorEntry: (data: { manufacturer: string; color_name: string; hex_color: string; material: string | null }) =>
    request<ColorCatalogEntry>('/inventory/colors', { method: 'POST', body: JSON.stringify(data) }),
  updateColorEntry: (id: number, data: { manufacturer: string; color_name: string; hex_color: string; material: string | null }) =>
    request<ColorCatalogEntry>(`/inventory/colors/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteColorEntry: (id: number) =>
    request<{ status: string }>(`/inventory/colors/${id}`, { method: 'DELETE' }),
  bulkDeleteColorEntries: (ids: number[]) =>
    request<{ deleted: number }>('/inventory/colors/bulk-delete', { method: 'POST', body: JSON.stringify({ ids }) }),
  resetColorCatalog: () =>
    request<{ status: string }>('/inventory/colors/reset', { method: 'POST' }),
  lookupColor: (manufacturer: string, colorName: string, material?: string) =>
    request<ColorLookupResult>(`/inventory/colors/lookup?manufacturer=${encodeURIComponent(manufacturer)}&color_name=${encodeURIComponent(colorName)}${material ? `&material=${encodeURIComponent(material)}` : ''}`),
  searchColors: (manufacturer?: string, material?: string) =>
    request<ColorCatalogEntry[]>(`/inventory/colors/search?${manufacturer ? `manufacturer=${encodeURIComponent(manufacturer)}` : ''}${manufacturer && material ? '&' : ''}${material ? `material=${encodeURIComponent(material)}` : ''}`),
  linkTagToSpool: (spoolId: number, data: { tag_uid?: string; tray_uuid?: string; tag_type?: string; data_origin?: string }) =>
    request<InventorySpool>(`/inventory/spools/${spoolId}/link-tag`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  getSpoolUsageHistory: (spoolId: number, limit = 50) =>
    request<SpoolUsageRecord[]>(`/inventory/spools/${spoolId}/usage?limit=${limit}`),
  getAllUsageHistory: (limit = 100, printerId?: number) =>
    request<SpoolUsageRecord[]>(`/inventory/usage?limit=${limit}${printerId ? `&printer_id=${printerId}` : ''}`),
  clearSpoolUsageHistory: (spoolId: number) =>
    request<{ status: string }>(`/inventory/spools/${spoolId}/usage`, { method: 'DELETE' }),
  syncWeightsFromAms: () =>
    request<{ synced: number; skipped: number }>('/inventory/sync-ams-weights', { method: 'POST' }),
  // Stock forecasting + shopping list (upstream #1184)
  getSkuSettings: () =>
    request<FilamentSkuSettings[]>('/inventory/sku-settings'),
  upsertSkuSettings: (data: Omit<FilamentSkuSettings, 'id'>) =>
    request<FilamentSkuSettings>('/inventory/sku-settings', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  getShoppingList: () =>
    request<ShoppingListItem[]>('/inventory/shopping-list'),
  addToShoppingList: (data: ShoppingListItemCreate) =>
    request<ShoppingListItem>('/inventory/shopping-list', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  removeFromShoppingList: (id: number) =>
    request<{ status: string }>(`/inventory/shopping-list/${id}`, { method: 'DELETE' }),
  clearShoppingList: () =>
    request<{ deleted: number }>('/inventory/shopping-list', { method: 'DELETE' }),
  updateShoppingListStatus: (id: number, status: 'pending' | 'purchased' | 'received') =>
    request<ShoppingListItem>(`/inventory/shopping-list/${id}/status`, {
      method: 'PATCH',
      body: JSON.stringify({ status }),
    }),
  getFilamentPresets: () =>
    request<SlicerSetting[]>('/cloud/filaments'),

  // Updates
  getVersion: () => request<VersionInfo>('/updates/version'),
  checkForUpdates: () => request<UpdateCheckResult>('/updates/check'),
  applyUpdate: (tagName?: string) =>
    request<{ success: boolean; message: string; target_ref?: string; status?: UpdateStatus; is_docker?: boolean; is_ha_addon?: boolean }>('/updates/apply', {
      method: 'POST',
      body: JSON.stringify(tagName ? { tag_name: tagName } : {}),
    }),
  getUpdateStatus: () => request<UpdateStatus>('/updates/status'),

  // Maintenance
  getMaintenanceTypes: () => request<MaintenanceType[]>('/maintenance/types'),
  createMaintenanceType: (data: MaintenanceTypeCreate) =>
    request<MaintenanceType>('/maintenance/types', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateMaintenanceType: (id: number, data: Partial<MaintenanceTypeCreate>) =>
    request<MaintenanceType>(`/maintenance/types/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteMaintenanceType: (id: number) =>
    request<{ status: string }>(`/maintenance/types/${id}`, { method: 'DELETE' }),
  restoreDefaultMaintenanceTypes: () =>
    request<{ restored: number }>(`/maintenance/types/restore-defaults`, { method: 'POST' }),
  getMaintenanceOverview: () => request<PrinterMaintenanceOverview[]>('/maintenance/overview'),
  getPrinterMaintenance: (printerId: number) =>
    request<PrinterMaintenanceOverview>(`/maintenance/printers/${printerId}`),
  updateMaintenanceItem: (itemId: number, data: { custom_interval_hours?: number | null; custom_interval_type?: 'hours' | 'days' | null; enabled?: boolean }) =>
    request<MaintenanceStatus>(`/maintenance/items/${itemId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  performMaintenance: (itemId: number, notes?: string) =>
    request<MaintenanceStatus>(`/maintenance/items/${itemId}/perform`, {
      method: 'POST',
      body: JSON.stringify({ notes }),
    }),
  getMaintenanceHistory: (itemId: number) =>
    request<MaintenanceHistory[]>(`/maintenance/items/${itemId}/history`),
  getAllMaintenanceHistory: (page?: number, perPage?: number, sortBy?: string, sortDir?: string, printerId?: number) => {
    const params = new URLSearchParams();
    if (page) params.set('page', String(page));
    if (perPage) params.set('per_page', String(perPage));
    if (sortBy) params.set('sort_by', sortBy);
    if (sortDir) params.set('sort_dir', sortDir);
    if (printerId) params.set('printer_id', String(printerId));
    return request<MaintenanceHistoryPage>(`/maintenance/history?${params}`);
  },
  getMaintenanceSummary: () => request<MaintenanceSummary>('/maintenance/summary'),
  setPrinterHours: (printerId: number, totalHours: number) =>
    request<{ printer_id: number; total_hours: number; archive_hours: number; offset_hours: number }>(
      `/maintenance/printers/${printerId}/hours?total_hours=${totalHours}`,
      { method: 'PATCH' }
    ),
  assignMaintenanceType: (printerId: number, typeId: number) =>
    request<MaintenanceStatus>(`/maintenance/printers/${printerId}/assign/${typeId}`, {
      method: 'POST',
    }),
  removeMaintenanceItem: (itemId: number) =>
    request<{ status: string }>(`/maintenance/items/${itemId}`, {
      method: 'DELETE',
    }),

  // Camera
  getCameraStreamToken: () =>
    request<{ token: string }>('/printers/camera/stream-token', { method: 'POST' }),
  getCameraStreamUrl: (printerId: number, fps = 10) =>
    withStreamToken(`${API_BASE}/printers/${printerId}/camera/stream?fps=${fps}`),
  getCameraSnapshotUrl: (printerId: number) =>
    withStreamToken(`${API_BASE}/printers/${printerId}/camera/snapshot`),
  testCameraConnection: (printerId: number) =>
    request<{ success: boolean; message?: string; error?: string }>(`/printers/${printerId}/camera/test`),
  getCameraStatus: (printerId: number) =>
    request<{ active: boolean; stalled: boolean }>(`/printers/${printerId}/camera/status`),

  // Plate Detection - Multi-reference calibration (stores up to 5 references per printer)
  checkPlateEmpty: (printerId: number, options?: { useExternal?: boolean; includeDebugImage?: boolean }) => {
    const params = new URLSearchParams();
    params.set('use_external', String(options?.useExternal ?? false));
    params.set('include_debug_image', String(options?.includeDebugImage ?? false));
    return request<PlateDetectionResult>(
      `/printers/${printerId}/camera/check-plate?${params.toString()}`
    );
  },
  getPlateDetectionStatus: (printerId: number) => {
    return request<PlateDetectionStatus & { chamber_light?: boolean }>(
      `/printers/${printerId}/camera/plate-detection/status`
    );
  },
  calibratePlateDetection: (printerId: number, options?: { label?: string; useExternal?: boolean }) => {
    const params = new URLSearchParams();
    if (options?.label) params.set('label', options.label);
    params.set('use_external', String(options?.useExternal ?? false));
    return request<CalibrationResult & { index: number }>(
      `/printers/${printerId}/camera/plate-detection/calibrate?${params.toString()}`,
      { method: 'POST' }
    );
  },
  deletePlateCalibration: (printerId: number) => {
    return request<CalibrationResult>(
      `/printers/${printerId}/camera/plate-detection/calibrate`,
      { method: 'DELETE' }
    );
  },
  getPlateReferences: (printerId: number) => {
    return request<{
      references: PlateReference[];
      max_references: number;
    }>(`/printers/${printerId}/camera/plate-detection/references`);
  },
  getPlateReferenceThumbnailUrl: (printerId: number, index: number) => {
    return withStreamToken(`${API_BASE}/printers/${printerId}/camera/plate-detection/references/${index}/thumbnail`);
  },
  updatePlateReferenceLabel: (printerId: number, index: number, label: string) => {
    const params = new URLSearchParams();
    params.set('label', label);
    return request<{ success: boolean; index: number; label: string }>(
      `/printers/${printerId}/camera/plate-detection/references/${index}?${params.toString()}`,
      { method: 'PUT' }
    );
  },
  deletePlateReference: (printerId: number, index: number) => {
    return request<{ success: boolean; message: string }>(
      `/printers/${printerId}/camera/plate-detection/references/${index}`,
      { method: 'DELETE' }
    );
  },

  // External Links
  getExternalLinks: () => request<ExternalLink[]>('/external-links/'),
  getExternalLink: (id: number) => request<ExternalLink>(`/external-links/${id}`),
  createExternalLink: (data: ExternalLinkCreate) =>
    request<ExternalLink>('/external-links/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateExternalLink: (id: number, data: ExternalLinkUpdate) =>
    request<ExternalLink>(`/external-links/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteExternalLink: (id: number) =>
    request<{ message: string }>(`/external-links/${id}`, { method: 'DELETE' }),
  reorderExternalLinks: (ids: number[]) =>
    request<ExternalLink[]>('/external-links/reorder', {
      method: 'PUT',
      body: JSON.stringify({ ids }),
    }),
  uploadExternalLinkIcon: async (id: number, file: File): Promise<ExternalLink> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/external-links/${id}/icon`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  deleteExternalLinkIcon: (id: number) =>
    request<ExternalLink>(`/external-links/${id}/icon`, { method: 'DELETE' }),
  getExternalLinkIconUrl: (id: number) => `${API_BASE}/external-links/${id}/icon`,

  // Projects
  getProjects: (status?: string) => {
    const params = new URLSearchParams();
    if (status) params.set('status', status);
    return request<ProjectListItem[]>(`/projects/?${params}`);
  },
  getProject: (id: number) => request<Project>(`/projects/${id}`),
  createProject: (data: ProjectCreate) =>
    request<Project>('/projects/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateProject: (id: number, data: ProjectUpdate) =>
    request<Project>(`/projects/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteProject: (id: number) =>
    request<{ message: string }>(`/projects/${id}`, { method: 'DELETE' }),
  getProjectArchives: (id: number, limit = 100, offset = 0) =>
    request<Archive[]>(`/projects/${id}/archives?limit=${limit}&offset=${offset}`),
  addArchivesToProject: (projectId: number, archiveIds: number[]) =>
    request<{ message: string }>(`/projects/${projectId}/add-archives`, {
      method: 'POST',
      body: JSON.stringify({ archive_ids: archiveIds }),
    }),
  removeArchivesFromProject: (projectId: number, archiveIds: number[]) =>
    request<{ message: string }>(`/projects/${projectId}/remove-archives`, {
      method: 'POST',
      body: JSON.stringify({ archive_ids: archiveIds }),
    }),
  addQueueItemsToProject: (projectId: number, queueItemIds: number[]) =>
    request<{ message: string }>(`/projects/${projectId}/add-queue`, {
      method: 'POST',
      body: JSON.stringify({ queue_item_ids: queueItemIds }),
    }),

  // Project Attachments
  uploadProjectAttachment: async (projectId: number, file: File): Promise<{
    status: string;
    filename: string;
    original_name: string;
    attachments: ProjectAttachment[];
  }> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/projects/${projectId}/attachments`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  getProjectAttachmentUrl: (projectId: number, filename: string) =>
    `${API_BASE}/projects/${projectId}/attachments/${encodeURIComponent(filename)}`,
  deleteProjectAttachment: (projectId: number, filename: string) =>
    request<{ status: string; message: string; attachments: ProjectAttachment[] | null }>(
      `/projects/${projectId}/attachments/${encodeURIComponent(filename)}`,
      { method: 'DELETE' }
    ),

  // B.2 (#1155) — Project cover image. The GET URL is consumed by an
  // <img src> tag, so it threads through withStreamToken() to satisfy
  // the camera-stream-token gate (the GET endpoint is RequireCameraStreamToken
  // for the same reason: <img> tags can't send Authorization headers).
  uploadProjectCoverImage: async (projectId: number, file: File): Promise<{
    status: string;
    filename: string;
    size: number;
  }> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/projects/${projectId}/cover-image`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  getProjectCoverImageUrl: (projectId: number) =>
    withStreamToken(`${API_BASE}/projects/${projectId}/cover-image`),
  deleteProjectCoverImage: (projectId: number) =>
    request<{ status: string }>(`/projects/${projectId}/cover-image`, { method: 'DELETE' }),

  // BOM (Bill of Materials)
  getProjectBOM: (projectId: number) =>
    request<BOMItem[]>(`/projects/${projectId}/bom`),
  createBOMItem: (projectId: number, data: BOMItemCreate) =>
    request<BOMItem>(`/projects/${projectId}/bom`, {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateBOMItem: (projectId: number, itemId: number, data: BOMItemUpdate) =>
    request<BOMItem>(`/projects/${projectId}/bom/${itemId}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteBOMItem: (projectId: number, itemId: number) =>
    request<{ status: string; message: string }>(`/projects/${projectId}/bom/${itemId}`, {
      method: 'DELETE',
    }),

  // Print Plan (per-project list of .3mf library files with copies + order)
  getProjectPrintPlan: (projectId: number) =>
    request<PrintPlanResponse>(`/projects/${projectId}/print-plan`),
  updatePrintPlanItem: (projectId: number, libraryFileId: number, copies: number) =>
    request<PrintPlanItem>(`/projects/${projectId}/print-plan/${libraryFileId}`, {
      method: 'PATCH',
      body: JSON.stringify({ copies }),
    }),
  reorderPrintPlan: (projectId: number, libraryFileIds: number[]) =>
    request<PrintPlanResponse>(`/projects/${projectId}/print-plan/reorder`, {
      method: 'POST',
      body: JSON.stringify({ library_file_ids: libraryFileIds }),
    }),

  // Templates
  getTemplates: () => request<ProjectListItem[]>('/projects/templates'),
  createTemplateFromProject: (projectId: number) =>
    request<Project>(`/projects/${projectId}/create-template`, { method: 'POST' }),
  createProjectFromTemplate: (templateId: number, name?: string) =>
    request<Project>(`/projects/from-template/${templateId}${name ? `?name=${encodeURIComponent(name)}` : ''}`, {
      method: 'POST',
    }),

  // Timeline
  getProjectTimeline: (projectId: number, limit = 50) =>
    request<TimelineEvent[]>(`/projects/${projectId}/timeline?limit=${limit}`),

  // Project Export/Import
  exportProjectJson: (projectId: number) =>
    request<ProjectExport>(`/projects/${projectId}/export?format=json`),
  importProject: (data: ProjectImport) =>
    request<Project>('/projects/import', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  importProjectFile: async (file: File): Promise<Project> => {
    const formData = new FormData();
    formData.append('file', file);
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/projects/import/file`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  exportProjectZip: async (projectId: number): Promise<{ blob: Blob; filename: string }> => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/projects/${projectId}/export`, {
      headers,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const contentDisposition = response.headers.get('Content-Disposition');
    const filename = parseContentDispositionFilename(contentDisposition) || `project_${projectId}.zip`;
    const blob = await response.blob();
    return { blob, filename };
  },

  // API Keys
  getAPIKeys: () => request<APIKey[]>('/api-keys/'),
  createAPIKey: (data: APIKeyCreate) =>
    request<APIKeyCreateResponse>('/api-keys/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateAPIKey: (id: number, data: APIKeyUpdate) =>
    request<APIKey>(`/api-keys/${id}`, {
      method: 'PATCH',
      body: JSON.stringify(data),
    }),
  deleteAPIKey: (id: number) =>
    request<{ message: string }>(`/api-keys/${id}`, { method: 'DELETE' }),

  // Long-lived camera-stream tokens (#1108)
  getLongLivedTokens: (userId?: number) =>
    request<LongLivedToken[]>(`/auth/tokens${userId !== undefined ? `?user_id=${userId}` : ''}`),
  getAllLongLivedTokens: () => request<LongLivedToken[]>('/auth/tokens/all'),
  createLongLivedToken: (data: LongLivedTokenCreate) =>
    request<LongLivedToken>('/auth/tokens', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  revokeLongLivedToken: (id: number) =>
    request<void>(`/auth/tokens/${id}`, { method: 'DELETE' }),

  // AMS History
  getAMSHistory: (printerId: number, amsId: number, hours = 24) =>
    request<AMSHistoryResponse>(`/ams-history/${printerId}/${amsId}?hours=${hours}`),

  // System Info
  getSystemInfo: () => request<SystemInfo>('/system/info'),
  getStorageUsage: (options?: { refresh?: boolean }) => {
    const params = new URLSearchParams();
    if (options?.refresh) {
      params.set('refresh', 'true');
    }
    const query = params.toString();
    return request<StorageUsageResponse>(`/system/storage-usage${query ? `?${query}` : ''}`);
  },

  // Library (File Manager)
  getLibraryFolders: () => request<LibraryFolderTree[]>('/library/folders'),
  createLibraryFolder: (data: LibraryFolderCreate) =>
    request<LibraryFolder>('/library/folders', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateLibraryFolder: (id: number, data: LibraryFolderUpdate) =>
    request<LibraryFolder>(`/library/folders/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteLibraryFolder: (id: number) =>
    request<{ status: string; message: string }>(`/library/folders/${id}`, { method: 'DELETE' }),
  createExternalFolder: (data: ExternalFolderCreate) =>
    request<LibraryFolder>('/library/folders/external', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  scanExternalFolder: (folderId: number) =>
    request<{ status: string; added: number; removed: number }>(`/library/folders/${folderId}/scan`, {
      method: 'POST',
    }),
  getLibraryFoldersByProject: (projectId: number) =>
    request<LibraryFolder[]>(`/library/folders/by-project/${projectId}`),
  getLibraryFoldersByArchive: (archiveId: number) =>
    request<LibraryFolder[]>(`/library/folders/by-archive/${archiveId}`),

  getLibraryFiles: (folderId?: number | null, includeRoot = true, projectId?: number) => {
    const params = new URLSearchParams();
    if (folderId !== undefined && folderId !== null) {
      params.set('folder_id', String(folderId));
    }
    if (projectId !== undefined) {
      params.set('project_id', String(projectId));
    }
    params.set('include_root', String(includeRoot));
    return request<LibraryFileListItem[]>(`/library/files?${params}`);
  },
  getLibraryFile: (id: number) => request<LibraryFile>(`/library/files/${id}`),
  uploadLibraryFile: async (
    file: File,
    folderId?: number | null,
    generateStlThumbnails: boolean = true
  ): Promise<LibraryFileUploadResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    const params = new URLSearchParams();
    if (folderId) params.set('folder_id', String(folderId));
    params.set('generate_stl_thumbnails', String(generateStlThumbnails));
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/library/files?${params}`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  extractZipFile: async (
    file: File,
    folderId?: number | null,
    preserveStructure: boolean = true,
    createFolderFromZip: boolean = false,
    generateStlThumbnails: boolean = true
  ): Promise<ZipExtractResponse> => {
    const formData = new FormData();
    formData.append('file', file);
    const params = new URLSearchParams();
    if (folderId) params.set('folder_id', String(folderId));
    params.set('preserve_structure', String(preserveStructure));
    params.set('create_folder_from_zip', String(createFolderFromZip));
    params.set('generate_stl_thumbnails', String(generateStlThumbnails));
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/library/files/extract-zip?${params}`, {
      method: 'POST',
      headers,
      body: formData,
    });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    return response.json();
  },
  updateLibraryFile: (id: number, data: LibraryFileUpdate) =>
    request<LibraryFile>(`/library/files/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteLibraryFile: (id: number) =>
    request<{ status: string; message: string; trashed: boolean }>(`/library/files/${id}`, { method: 'DELETE' }),

  // m044: drop a single (file, project) pivot row without read-modify-write
  // on the whole list. Used by ProjectDetailPage's "remove from this project"
  // affordance. Idempotent: missing pivot is a no-op (204).
  removeLibraryFileFromProject: (fileId: number, projectId: number) =>
    request<void>(`/library/files/${fileId}/projects/${projectId}`, { method: 'DELETE' }),

  // m044: symmetric for folders.
  removeLibraryFolderFromProject: (folderId: number, projectId: number) =>
    request<void>(`/library/folders/${folderId}/projects/${projectId}`, { method: 'DELETE' }),

  // ========== Library Trash (#1008) ==========
  previewLibraryPurge: (olderThanDays: number, includeNeverPrinted: boolean = true) =>
    request<LibraryPurgePreview>(
      `/library/purge/preview?older_than_days=${olderThanDays}&include_never_printed=${includeNeverPrinted}`,
    ),
  executeLibraryPurge: (olderThanDays: number, includeNeverPrinted: boolean = true) =>
    request<{ moved_to_trash: number }>('/library/purge', {
      method: 'POST',
      body: JSON.stringify({ older_than_days: olderThanDays, include_never_printed: includeNeverPrinted }),
    }),
  listLibraryTrash: (limit: number = 100, offset: number = 0) =>
    request<LibraryTrashListResponse>(`/library/trash?limit=${limit}&offset=${offset}`),
  restoreLibraryTrash: (fileId: number) =>
    request<{ status: string; id: number }>(`/library/trash/${fileId}/restore`, { method: 'POST' }),
  hardDeleteLibraryTrash: (fileId: number) =>
    request<{ status: string }>(`/library/trash/${fileId}`, { method: 'DELETE' }),
  emptyLibraryTrash: () =>
    request<{ deleted: number; skipped_pinned: number }>('/library/trash', { method: 'DELETE' }),
  getLibraryTrashSettings: () =>
    request<LibraryTrashSettings>('/library/trash/settings'),
  updateLibraryTrashSettings: (body: LibraryTrashSettings) =>
    request<LibraryTrashSettings>('/library/trash/settings', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),
  getLibraryAutoPurgeStatus: () =>
    request<LibraryAutoPurgeStatus>('/library/trash/auto-purge/status'),

  // ========== Archive trash (#1008 follow-up) ==========
  listArchiveTrash: (limit: number = 100, offset: number = 0) =>
    request<ArchiveTrashListResponse>(`/archives/trash?limit=${limit}&offset=${offset}`),
  restoreArchiveTrash: (archiveId: number) =>
    request<{ status: string; id: number }>(`/archives/trash/${archiveId}/restore`, { method: 'POST' }),
  hardDeleteArchiveTrash: (archiveId: number) =>
    request<{ status: string }>(`/archives/trash/${archiveId}`, { method: 'DELETE' }),
  emptyArchiveTrash: () => request<{ deleted: number }>('/archives/trash', { method: 'DELETE' }),
  getArchiveTrashSettings: () => request<ArchiveTrashSettings>('/archives/trash/settings'),
  updateArchiveTrashSettings: (body: ArchiveTrashSettings) =>
    request<ArchiveTrashSettings>('/archives/trash/settings', {
      method: 'PUT',
      body: JSON.stringify(body),
    }),

  // Library file notes (gh#3)
  getLibraryFileNotes: (fileId: number) =>
    request<LibraryFileNote[]>(`/library/files/${fileId}/notes`),
  createLibraryFileNote: (fileId: number, body: string) =>
    request<LibraryFileNote>(`/library/files/${fileId}/notes`, {
      method: 'POST',
      body: JSON.stringify({ body }),
    }),
  updateLibraryFileNote: (noteId: number, body: string) =>
    request<LibraryFileNote>(`/library/notes/${noteId}`, {
      method: 'PATCH',
      body: JSON.stringify({ body }),
    }),
  deleteLibraryFileNote: (noteId: number) =>
    request<{ success: boolean }>(`/library/notes/${noteId}`, { method: 'DELETE' }),

  getLibraryFileDownloadUrl: (id: number) => `${API_BASE}/library/files/${id}/download`,
  createLibrarySlicerToken: (fileId: number) =>
    request<{ token: string }>(`/library/files/${fileId}/slicer-token`, { method: 'POST' }),
  getLibrarySlicerDownloadUrl: (fileId: number, token: string, filename: string) =>
    `${API_BASE}/library/files/${fileId}/dl/${token}/${encodeURIComponent(filename)}`,
  downloadLibraryFile: async (id: number, filename?: string): Promise<void> => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/library/files/${id}/download`, { headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const disposition = response.headers.get('Content-Disposition');
    const downloadFilename = parseContentDispositionFilename(disposition) || filename || `file_${id}`;
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = downloadFilename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },
  getLibraryFileThumbnailUrl: (id: number) => `${API_BASE}/library/files/${id}/thumbnail`,
  getLibraryFilePlateThumbnail: (id: number, plateIndex: number) =>
    `${API_BASE}/library/files/${id}/plate-thumbnail/${plateIndex}`,
  getLibraryFileGcodeUrl: (id: number, plateId?: number | null) =>
    `${API_BASE}/library/files/${id}/gcode${plateId != null ? `?plate_id=${plateId}` : ''}`,
  moveLibraryFiles: (fileIds: number[], folderId: number | null) =>
    request<{ status: string; moved: number }>('/library/files/move', {
      method: 'POST',
      body: JSON.stringify({ file_ids: fileIds, folder_id: folderId }),
    }),
  bulkDeleteLibrary: (fileIds: number[], folderIds: number[]) =>
    request<{ deleted_files: number; deleted_folders: number }>('/library/bulk-delete', {
      method: 'POST',
      body: JSON.stringify({ file_ids: fileIds, folder_ids: folderIds }),
    }),
  getLibraryStats: () => request<LibraryStats>('/library/stats'),
  batchGenerateStlThumbnails: (options: {
    file_ids?: number[];
    folder_id?: number;
    all_missing?: boolean;
  }) =>
    request<BatchThumbnailResponse>('/library/generate-stl-thumbnails', {
      method: 'POST',
      body: JSON.stringify(options),
    }),
  addLibraryFilesToQueue: (fileIds: number[]) =>
    request<AddToQueueResponse>('/library/files/add-to-queue', {
      method: 'POST',
      body: JSON.stringify({ file_ids: fileIds }),
    }),
  printLibraryFile: (
    fileId: number,
    printerId: number,
    options?: {
      plate_id?: number;
      plate_name?: string;
      ams_mapping?: number[];
      bed_levelling?: boolean;
      flow_cali?: boolean;
      layer_inspect?: boolean;
      timelapse?: boolean;
      use_ams?: boolean;
      mesh_mode_fast_check?: boolean;
      execute_swap_macros?: boolean;
      swap_macro_events?: string[] | null;
      quantity?: number;
      project_id?: number;
      cleanup_library_after_dispatch?: boolean;
    }
  ) =>
    request<BackgroundDispatchResponse>(
      `/library/files/${fileId}/print?printer_id=${printerId}`,
      {
        method: 'POST',
        body: options ? JSON.stringify(options) : undefined,
      }
    ),
  cancelBackgroundDispatchJob: (jobId: number) =>
    request<{
      status: 'cancelled' | 'cancelling';
      job_id: number;
      source_name: string;
      printer_id: number;
      printer_name: string;
    }>(`/background-dispatch/${jobId}`, {
      method: 'DELETE',
    }),
  getLibraryFilePlates: (fileId: number) =>
    request<LibraryFilePlatesResponse>(`/library/files/${fileId}/plates`),
  getLibraryFileFilamentRequirements: (fileId: number, plateId?: number, requestId?: string) => {
    const params = new URLSearchParams();
    if (plateId !== undefined) params.set('plate_id', String(plateId));
    if (requestId !== undefined) params.set('request_id', requestId);
    const qs = params.toString();
    return request<{
      file_id: number;
      filename: string;
      filaments: Array<{
        slot_id: number;
        type: string;
        color: string;
        used_grams: number;
        used_meters: number;
        used_in_plate?: boolean;
      }>;
    }>(`/library/files/${fileId}/filament-requirements${qs ? `?${qs}` : ''}`);
  },

  // Git Backup
  getGitBackupConfig: () =>
    request<GitBackupConfig | null>('/git-backup/config'),

  saveGitBackupConfig: (config: GitBackupConfigCreate) =>
    request<GitBackupConfig>('/git-backup/config', {
      method: 'POST',
      body: JSON.stringify(config),
    }),

  updateGitBackupConfig: (config: Partial<GitBackupConfigCreate>) =>
    request<GitBackupConfig>('/git-backup/config', {
      method: 'PATCH',
      body: JSON.stringify(config),
    }),

  deleteGitBackupConfig: () =>
    request<{ message: string }>('/git-backup/config', { method: 'DELETE' }),

  testGitConnection: (repoUrl: string, token: string, provider?: string, apiBaseUrl?: string) => {
    const params = new URLSearchParams({ repo_url: repoUrl, token });
    if (provider) params.set('provider', provider);
    if (apiBaseUrl) params.set('api_base_url', apiBaseUrl);
    return request<GitTestConnectionResponse>(
      `/git-backup/test?${params.toString()}`,
      { method: 'POST' }
    );
  },

  testStoredGitConnection: () =>
    request<GitTestConnectionResponse>('/git-backup/test-stored', { method: 'POST' }),

  triggerGitBackup: () =>
    request<GitBackupTriggerResponse>('/git-backup/run', { method: 'POST' }),

  getGitBackupStatus: () =>
    request<GitBackupStatus>('/git-backup/status'),

  getGitBackupLogs: (limit: number = 50) =>
    request<GitBackupLog[]>(`/git-backup/logs?limit=${limit}`),

  clearGitBackupLogs: (keepLast: number = 10) =>
    request<{ deleted: number; message: string }>(`/git-backup/logs?keep_last=${keepLast}`, { method: 'DELETE' }),

  // Scheduled Local Backup (#884)
  getLocalBackupStatus: () =>
    request<LocalBackupStatus>('/local-backup/status'),
  triggerLocalBackup: () =>
    request<LocalBackupRunResponse>('/local-backup/run', { method: 'POST' }),
  listLocalBackups: () =>
    request<LocalBackupFile[]>('/local-backup/backups'),
  getLocalBackupDownloadUrl: (filename: string) =>
    `${API_BASE}/local-backup/backups/${encodeURIComponent(filename)}/download`,
  restoreLocalBackup: (filename: string) =>
    request<{ success?: boolean; message?: string }>(
      `/local-backup/backups/${encodeURIComponent(filename)}/restore`,
      { method: 'POST' }
    ),
  deleteLocalBackup: (filename: string) =>
    request<LocalBackupDeleteResponse>(
      `/local-backup/backups/${encodeURIComponent(filename)}`,
      { method: 'DELETE' }
    ),

  // Obico AI failure detection (#172)
  getObicoStatus: () =>
    request<ObicoStatus>('/obico/status'),

  testObicoConnection: (url: string) =>
    request<ObicoTestConnection>('/obico/test-connection', {
      method: 'POST',
      body: JSON.stringify({ url }),
    }),

  // Server-side slicing (B.4) — Phase 2 of 0.5.x cycle
  sliceLibraryFile: (fileId: number, body: SliceRequest) =>
    request<SliceJobEnqueueResponse>(`/library/files/${fileId}/slice`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  sliceArchive: (archiveId: number, body: SliceRequest) =>
    request<SliceJobEnqueueResponse>(`/archives/${archiveId}/slice`, {
      method: 'POST',
      body: JSON.stringify(body),
    }),
  getSliceJob: (jobId: number) =>
    request<SliceJobState>(`/slice-jobs/${jobId}`),
  // Unified slicer-preset listing — cloud + local + standard, deduped by name.
  // Drives the SliceModal preset dropdowns. See backend
  // routes/slicer_presets.py for the priority + dedup rules.
  getSlicerPresets: () =>
    request<UnifiedPresetsResponse>('/slicer/presets'),
  // Per-request progress proxy used by the SliceModal's filament-discovery
  // preview slice (the sidecar's CORS allowlist + same-origin policy stop
  // the browser from hitting /slice/progress/{id} directly).
  getPreviewSliceProgress: (requestId: string) =>
    request<SliceJobProgress | null>(`/slicer/preview-progress/${requestId}`),
  // Reachability probe for a single sidecar — used by the SliceModal to
  // disable a radio option when the picked slicer is offline. The backend
  // caches results for 30 s per (kind, url) so render-time polls don't hit
  // the wire on every dropdown open.
  getSlicerHealth: (slicer: 'orcaslicer' | 'bambu_studio') =>
    request<SlicerHealth>(`/slicer/health/${slicer}`),

  // Slicer Preset Bundles (.bbscfg) — pick presets from a stored bundle
  // sidecar-side instead of resolving cloud/local/standard PresetRefs every
  // slice. SliceModal renders the bundle picker only when this list is
  // non-empty; falls back to PresetRef triplet path when empty.
  listSlicerBundles: () =>
    request<SlicerBundle[]>('/slicer/bundles'),
  getSlicerBundle: (id: string) =>
    request<SlicerBundle>(`/slicer/bundles/${encodeURIComponent(id)}`),
  importSlicerBundle: (file: File) => {
    const formData = new FormData();
    formData.append('file', file);
    return fetch(`${API_BASE}/slicer/bundles`, {
      method: 'POST',
      headers: authToken ? { Authorization: `Bearer ${authToken}` } : {},
      body: formData,
    }).then(async (response) => {
      if (!response.ok) {
        // Pull `detail` out of the FastAPI JSON envelope so the toast
        // shows "Invalid file type. ..." instead of the raw
        // `{"detail":"..."}` body. We read text() once (body is a
        // one-shot stream — calling json() then text() would throw
        // "body already used") and try JSON.parse ourselves.
        const text = await response.text().catch(() => '');
        let detail: string | null = null;
        try {
          const parsed = JSON.parse(text);
          if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
        } catch {
          // not JSON — keep raw text
        }
        throw new Error(detail || text || `HTTP ${response.status}`);
      }
      return response.json() as Promise<SlicerBundle>;
    });
  },
  deleteSlicerBundle: (id: string) =>
    request<void>(`/slicer/bundles/${encodeURIComponent(id)}`, { method: 'DELETE' }),

  // Local Presets (OrcaSlicer imports)
  getLocalPresets: () =>
    request<LocalPresetsResponse>('/local-presets/'),
  getLocalPresetDetail: (id: number) =>
    request<LocalPresetDetail>(`/local-presets/${id}`),
  importLocalPresets: (formData: FormData) =>
    fetch(`${API_BASE}/local-presets/import`, {
      method: 'POST',
      headers: authToken ? { 'Authorization': `Bearer ${authToken}` } : {},
      body: formData,
    }).then(async (res) => {
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(err.detail || `HTTP ${res.status}`);
      }
      return res.json() as Promise<ImportResponse>;
    }),
  createLocalPreset: (data: { name: string; preset_type: string; setting: Record<string, unknown> }) =>
    request<LocalPreset>('/local-presets/', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  updateLocalPreset: (id: number, data: { name?: string; setting?: Record<string, unknown> }) =>
    request<LocalPreset>(`/local-presets/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),
  deleteLocalPreset: (id: number) =>
    request<{ success: boolean }>(`/local-presets/${id}`, { method: 'DELETE' }),
  refreshBaseProfileCache: () =>
    request<{ refreshed: number; failed: number; total: number }>('/local-presets/base-cache/refresh', { method: 'POST' }),

  // Telegram Chats
  getTelegramChats: () => request<TelegramChat[]>('/telegram/chats'),
  createTelegramChat: (data: TelegramChatCreate) =>
    request<TelegramChat>('/telegram/chats', { method: 'POST', body: JSON.stringify(data) }),
  updateTelegramChat: (id: number, data: TelegramChatUpdate) =>
    request<TelegramChat>(`/telegram/chats/${id}`, { method: 'PUT', body: JSON.stringify(data) }),
  deleteTelegramChat: (id: number) =>
    request<void>(`/telegram/chats/${id}`, { method: 'DELETE' }),
  testTelegramChat: (id: number) =>
    request<{ status: string }>(`/telegram/chats/${id}/test`, { method: 'POST' }),
  getTelegramEvents: () => request<NotifyEventInfo[]>('/telegram/events'),

  // Per-(user, printer-model) PrintModal toggle preferences. The model
  // string is URI-encoded so models with characters like spaces or "/"
  // round-trip correctly.
  getPrintOptionsPreference: (printerModel: string) =>
    request<PrintOptionsPreferenceResponse>(
      `/print-option-preferences/${encodeURIComponent(printerModel)}`,
    ),
  upsertPrintOptionsPreference: (printerModel: string, data: PrintOptionsPreferenceData) =>
    request<PrintOptionsPreferenceResponse>(
      `/print-option-preferences/${encodeURIComponent(printerModel)}`,
      { method: 'PUT', body: JSON.stringify(data) },
    ),

  // Admin preference ops — Settings → Print → Saved Profiles widget.
  // All gated server-side on USERS_READ (list) / USERS_UPDATE (writes).
  listAllPrintOptionsPreferences: () =>
    request<PrintOptionsPreferenceAdminEntry[]>('/print-option-preferences/admin/list'),
  adminUpsertPrintOptionsPreference: (
    userId: number,
    printerModel: string,
    data: PrintOptionsPreferenceData,
  ) =>
    request<PrintOptionsPreferenceResponse>(
      `/print-option-preferences/admin/${userId}/${encodeURIComponent(printerModel)}`,
      { method: 'PUT', body: JSON.stringify(data) },
    ),
  adminDeletePrintOptionsPreference: (userId: number, printerModel: string) =>
    request<void>(
      `/print-option-preferences/admin/${userId}/${encodeURIComponent(printerModel)}`,
      { method: 'DELETE' },
    ),
  adminCopyPrintOptionsPreference: (body: PrintOptionsPreferenceCopyRequest) =>
    request<PrintOptionsPreferenceResponse>('/print-option-preferences/admin/copy', {
      method: 'POST',
      body: JSON.stringify(body),
    }),
};

// Telegram Chat types
export interface TelegramChat {
  id: number;
  chat_id: number;
  label: string | null;
  group_id: number | null;
  group_name: string | null;
  user_id: number | null;
  username: string | null;
  is_active: boolean;
  notify_events: string[] | null;
  daily_digest: boolean;
  quiet_hours_enabled: boolean;
  quiet_hours_start: string | null;
  quiet_hours_end: string | null;
  created_at: string;
  updated_at: string;
}

export interface TelegramChatCreate {
  chat_id: number;
  label?: string | null;
  group_id?: number | null;
  user_id?: number | null;
  is_active?: boolean;
  notify_events?: string[] | null;
  daily_digest?: boolean;
  quiet_hours_enabled?: boolean;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
}

export interface TelegramChatUpdate {
  label?: string | null;
  group_id?: number | null;
  user_id?: number | null;
  is_active?: boolean;
  notify_events?: string[] | null;
  daily_digest?: boolean;
  quiet_hours_enabled?: boolean;
  quiet_hours_start?: string | null;
  quiet_hours_end?: string | null;
}

export interface NotifyEventInfo {
  event_type: string;
  category: string;
  label: string;
  default: boolean;
}

// AMS History types
export interface AMSHistoryPoint {
  recorded_at: string;
  humidity: number | null;
  humidity_raw: number | null;
  temperature: number | null;
}

export interface AMSHistoryResponse {
  printer_id: number;
  ams_id: number;
  data: AMSHistoryPoint[];
  min_humidity: number | null;
  max_humidity: number | null;
  avg_humidity: number | null;
  min_temperature: number | null;
  max_temperature: number | null;
  avg_temperature: number | null;
}

// System Info types
export interface SystemInfo {
  app: {
    version: string;
    base_dir: string;
    archive_dir: string;
  };
  database: {
    engine: 'SQLite' | 'PostgreSQL';
    version: string;
    archives: number;
    archives_completed: number;
    archives_failed: number;
    archives_printing: number;
    printers: number;
    filaments: number;
    projects: number;
    smart_plugs: number;
    total_print_time_seconds: number;
    total_print_time_formatted: string;
    total_filament_grams: number;
    total_filament_kg: number;
  };
  printers: {
    total: number;
    connected: number;
    connected_list: Array<{
      id: number;
      name: string;
      state: string;
      model: string;
    }>;
  };
  storage: {
    archive_size_bytes: number;
    archive_size_formatted: string;
    database_size_bytes: number;
    database_size_formatted: string;
    disk_total_bytes: number;
    disk_total_formatted: string;
    disk_used_bytes: number;
    disk_used_formatted: string;
    disk_free_bytes: number;
    disk_free_formatted: string;
    disk_percent_used: number;
  };
  system: {
    platform: string;
    platform_release: string;
    platform_version: string;
    architecture: string;
    hostname: string;
    python_version: string;
    uptime_seconds: number;
    uptime_formatted: string;
    boot_time: string;
  };
  memory: {
    total_bytes: number;
    total_formatted: string;
    available_bytes: number;
    available_formatted: string;
    used_bytes: number;
    used_formatted: string;
    percent_used: number;
  };
  cpu: {
    count: number;
    count_logical: number;
    percent: number;
  };
}

export interface StorageUsageCategory {
  key: string;
  label: string;
  bytes: number;
  formatted: string;
  percent_of_total: number;
}

export interface StorageUsageOtherItem {
  bucket: string;
  label: string;
  kind: 'system' | 'data';
  deletable: boolean;
  bytes: number;
  formatted: string;
  percent_of_total: number;
}

export interface StorageUsageResponse {
  roots: string[];
  total_bytes: number;
  total_formatted: string;
  categories: StorageUsageCategory[];
  other_breakdown: StorageUsageOtherItem[];
  scan_errors: number;
  generated_at: string;
  cache: {
    hit: boolean;
    age_seconds: number;
    max_age_seconds: number;
  };
}

// Library (File Manager) types

// m044: lightweight project reference embedded in folder/file responses.
// Carries enough for the UI to render the colored project chip without a
// follow-up fetch. Mirrors backend `ProjectRef` schema.
export interface ProjectRef {
  id: number;
  name: string;
  color: string | null;
}

export interface LibraryFolderTree {
  id: number;
  name: string;
  parent_id: number | null;
  // m044: M2M project links. Empty array = unattached.
  projects: ProjectRef[];
  archive_id: number | null;
  archive_name: string | null;
  is_external: boolean;
  external_path: string | null;
  external_readonly: boolean;
  file_count: number;
  children: LibraryFolderTree[];
}

export interface LibraryFolder {
  id: number;
  name: string;
  parent_id: number | null;
  projects: ProjectRef[];
  archive_id: number | null;
  archive_name: string | null;
  is_external: boolean;
  external_path: string | null;
  external_readonly: boolean;
  external_show_hidden: boolean;
  file_count: number;
  created_at: string;
  updated_at: string;
}

export interface LibraryFolderCreate {
  name: string;
  parent_id?: number | null;
  // m044: list of project IDs to associate the folder with on creation.
  project_ids?: number[];
  archive_id?: number | null;
}

export interface ExternalFolderCreate {
  name: string;
  external_path: string;
  readonly?: boolean;
  show_hidden?: boolean;
  parent_id?: number | null;
}

export interface LibraryFolderUpdate {
  name?: string;
  parent_id?: number | null;
  // m044: undefined = leave links untouched, [] = unlink from every
  // project, otherwise replace the whole list.
  project_ids?: number[];
  archive_id?: number | null;  // 0 to unlink
}

export interface LibraryFileDuplicate {
  id: number;
  filename: string;
  folder_id: number | null;
  folder_name: string | null;
  created_at: string;
}

// Library trash (#1008)
export interface LibraryTrashItem {
  id: number;
  filename: string;
  file_size: number;
  thumbnail_path: string | null;
  folder_id: number | null;
  folder_name: string | null;
  created_by_id: number | null;
  created_by_username: string | null;
  deleted_at: string;
  auto_purge_at: string;
}

export interface LibraryTrashListResponse {
  items: LibraryTrashItem[];
  total: number;
  retention_days: number;
}

export interface LibraryPurgePreview {
  count: number;
  total_bytes: number;
  sample_filenames: string[];
  older_than_days: number;
  include_never_printed: boolean;
}

export interface LibraryAutoPurgeLastRun {
  started_at: string;
  finished_at: string | null;
  /** -1 means "ran but the count was lost on process restart". */
  moved: number;
}

export interface LibraryAutoPurgeStatus {
  enabled: boolean;
  days: number;
  include_never_printed: boolean;
  last_run: LibraryAutoPurgeLastRun | null;
  next_run_at: string | null;
}

export interface LibraryTrashSettings {
  retention_days: number;
  auto_purge_enabled: boolean;
  auto_purge_days: number;
  auto_purge_include_never_printed: boolean;
}

export interface ArchiveTrashItem {
  id: number;
  filename: string;
  print_name: string | null;
  file_size: number | null;
  thumbnail_path: string | null;
  printer_id: number | null;
  project_id: number | null;
  status: string | null;
  created_by_id: number | null;
  created_by_username: string | null;
  deleted_at: string;
  auto_purge_at: string;
}

export interface ArchiveTrashListResponse {
  items: ArchiveTrashItem[];
  total: number;
  retention_days: number;
}

export interface ArchiveTrashSettings {
  retention_days: number;
}

export interface LibraryFile {
  id: number;
  folder_id: number | null;
  folder_name: string | null;
  // m044: M2M project links. Empty array = unattached.
  projects: ProjectRef[];
  is_external: boolean;
  filename: string;
  file_path: string;
  file_type: string;
  // Composite tag array (m036) — drives badges + chip-row filter in the
  // file-manager. See ``compute_file_tags`` on the backend for the value
  // vocabulary. Examples:
  //   ['gcode']                         — raw .gcode upload
  //   ['gcode', '3mf', 'sliced']        — server-side slicer output
  //   ['3mf', 'multiplate']             — un-sliced multi-plate 3MF
  //   ['stl', 'makerworld']             — STL pulled from MakerWorld
  //   ['gcode', '3mf', 'multiplate', 'swap', 'sliced']  — the works
  file_tags: string[];
  file_size: number;
  file_hash: string | null;
  thumbnail_path: string | null;
  metadata: Record<string, unknown> | null;
  last_printed_at: string | null;
  notes: string | null;
  duplicates: LibraryFileDuplicate[] | null;
  duplicate_count: number;
  // User tracking (Issue #206)
  created_by_id: number | null;
  created_by_username: string | null;
  created_at: string;
  updated_at: string;
  // Metadata fields
  print_name: string | null;
  print_time_seconds: number | null;
  filament_used_grams: number | null;
  object_count: number | null;
  sliced_for_model: string | null;
  swap_compatible: boolean;
  // Provenance (m033) — populated for MakerWorld imports + slicer outputs.
  // ``source_type`` ∈ {"makerworld", "sliced", ...}; ``source_url`` is the
  // canonical link (e.g. MakerWorld profile URL). NULL for plain uploads.
  source_type?: string | null;
  source_url?: string | null;
}

export interface LibraryFileListItem {
  id: number;
  folder_id: number | null;
  // m044: M2M project IDs only (names omitted to keep list payload small —
  // resolve names from a global ``projects`` query when rendering).
  project_ids: number[];
  is_external: boolean;
  filename: string;
  file_type: string;
  // Composite tag array — see ``LibraryFile.file_tags``.
  file_tags: string[];
  file_size: number;
  thumbnail_path: string | null;
  duplicate_count: number;
  // User tracking (Issue #206)
  created_by_id: number | null;
  created_by_username: string | null;
  created_at: string;
  print_name: string | null;
  print_time_seconds: number | null;
  filament_used_grams: number | null;
  object_count: number | null;
  sliced_for_model: string | null;
  swap_compatible: boolean;
  // True iff the 3MF carries 2+ plates (extracted at upload / m023 backfill).
  // Used to gate gallery rendering — single-plate files skip the per-card
  // gallery fetch entirely.
  is_multi_plate?: boolean;
  // Provenance (m033) — same semantics as ``LibraryFile``.
  source_type?: string | null;
  source_url?: string | null;
  notes_count: number;
}

// gh#3 - User-authored notes attached to library files
export interface LibraryFileNote {
  id: number;
  library_file_id: number;
  user_id: number | null;
  user_username: string | null;
  body: string;
  created_at: string;
  updated_at: string;
  can_edit: boolean;
}

export const LIBRARY_FILE_NOTE_MAX_LENGTH = 1000;

export interface LibraryFileUpdate {
  filename?: string;
  folder_id?: number | null;
  // m044: undefined = leave untouched, [] = unlink from every project,
  // otherwise replace the whole list.
  project_ids?: number[];
  notes?: string | null;
}

export interface LibraryFileUploadResponse {
  id: number;
  filename: string;
  file_type: string;
  file_size: number;
  thumbnail_path: string | null;
  duplicate_of: number | null;
  metadata: Record<string, unknown> | null;
}

export interface LibraryStats {
  total_files: number;
  total_folders: number;
  total_size_bytes: number;
  files_by_type: Record<string, number>;
  disk_free_bytes: number;
  disk_total_bytes: number;
  disk_used_bytes: number;
}

export interface ZipExtractResult {
  filename: string;
  file_id: number;
  folder_id: number | null;
}

export interface ZipExtractError {
  filename: string;
  error: string;
}

export interface ZipExtractResponse {
  extracted: number;
  folders_created: number;
  files: ZipExtractResult[];
  errors: ZipExtractError[];
}

// STL Thumbnail Generation types
export interface BatchThumbnailResult {
  file_id: number;
  filename: string;
  success: boolean;
  error?: string | null;
}

export interface BatchThumbnailResponse {
  processed: number;
  succeeded: number;
  failed: number;
  results: BatchThumbnailResult[];
}

// Library Queue types
export interface AddToQueueResult {
  file_id: number;
  filename: string;
  queue_item_id: number;
  archive_id: number;
}

export interface AddToQueueError {
  file_id: number;
  filename: string;
  error: string;
}

export interface AddToQueueResponse {
  added: AddToQueueResult[];
  errors: AddToQueueError[];
}

// Discovery types
export interface DiscoveredPrinter {
  serial: string;
  name: string;
  ip_address: string;
  model: string | null;
  discovered_at: string | null;
}

export interface DiscoveryStatus {
  running: boolean;
}

export interface DiscoveryInfo {
  is_docker: boolean;
  ssdp_running: boolean;
  scan_running: boolean;
  subnets: string[];
}

export interface SubnetScanStatus {
  running: boolean;
  scanned: number;
  total: number;
}

// Discovery API
export const discoveryApi = {
  getInfo: () => request<DiscoveryInfo>('/discovery/info'),

  getStatus: () => request<DiscoveryStatus>('/discovery/status'),

  startDiscovery: (duration: number = 10) =>
    request<DiscoveryStatus>(`/discovery/start?duration=${duration}`, { method: 'POST' }),

  stopDiscovery: () =>
    request<DiscoveryStatus>('/discovery/stop', { method: 'POST' }),

  getDiscoveredPrinters: () =>
    request<DiscoveredPrinter[]>('/discovery/printers'),

  // Subnet scanning (for Docker environments)
  startSubnetScan: (subnet: string, timeout: number = 1.0) =>
    request<SubnetScanStatus>('/discovery/scan', {
      method: 'POST',
      body: JSON.stringify({ subnet, timeout }),
    }),

  getScanStatus: () => request<SubnetScanStatus>('/discovery/scan/status'),

  stopSubnetScan: () =>
    request<SubnetScanStatus>('/discovery/scan/stop', { method: 'POST' }),
};

// Virtual Printer types
// Three supported modes after the m002 migration purged the legacy
// ``immediate`` / ``review`` / ``queue`` values.
export type VirtualPrinterMode = 'print_queue' | 'auto_queue' | 'file_manager' | 'proxy';

export interface VirtualPrinterProxyStatus {
  running: boolean;
  target_host: string;
  ftp_port: number;
  mqtt_port: number;
  ftp_connections: number;
  mqtt_connections: number;
}

export interface VirtualPrinterStatus {
  enabled: boolean;
  running: boolean;
  mode: VirtualPrinterMode;
  name: string;
  serial: string;
  model: string;
  model_name: string;
  pending_files: number;
  target_printer_ip?: string;  // For proxy mode
  proxy?: VirtualPrinterProxyStatus;  // For proxy mode
}

export interface VirtualPrinterSettings {
  enabled: boolean;
  access_code_set: boolean;
  mode: VirtualPrinterMode;
  model: string;
  target_printer_id: number | null;  // For proxy mode
  remote_interface_ip: string | null;  // For SSDP proxy across networks
  // 'metadata' uses the 3MF's embedded print_name (creator-baked title);
  // 'filename' uses the FTP-uploaded filename so renames in BambuStudio's
  // "send to printer" dialog surface in the archive (#1152, audit B.14).
  archive_name_source: 'metadata' | 'filename';
  status: VirtualPrinterStatus;
}

export interface NetworkInterface {
  name: string;
  ip: string;
  netmask: string;
  subnet: string;
  is_alias?: boolean;
  label?: string;
}

export interface VirtualPrinterModels {
  models: Record<string, string>;  // SSDP code -> display name
  default: string;
}

// Virtual Printer API
export const virtualPrinterApi = {
  getSettings: () => request<VirtualPrinterSettings>('/settings/virtual-printer'),

  getModels: () => request<VirtualPrinterModels>('/settings/virtual-printer/models'),

  updateSettings: (data: {
    enabled?: boolean;
    access_code?: string;
    mode?: VirtualPrinterMode;
    model?: string;
    target_printer_id?: number;
    remote_interface_ip?: string;
    archive_name_source?: 'metadata' | 'filename';
  }) => {
    const params = new URLSearchParams();
    if (data.enabled !== undefined) params.set('enabled', String(data.enabled));
    if (data.access_code !== undefined) params.set('access_code', data.access_code);
    if (data.mode !== undefined) params.set('mode', data.mode);
    if (data.model !== undefined) params.set('model', data.model);
    if (data.target_printer_id !== undefined) params.set('target_printer_id', String(data.target_printer_id));
    if (data.remote_interface_ip !== undefined) params.set('remote_interface_ip', data.remote_interface_ip);
    if (data.archive_name_source !== undefined) params.set('archive_name_source', data.archive_name_source);

    return request<VirtualPrinterSettings>(`/settings/virtual-printer?${params.toString()}`, {
      method: 'PUT',
    });
  },
};

// Multi Virtual Printer API
export interface VirtualPrinterConfig {
  id: number;
  name: string;
  enabled: boolean;
  mode: VirtualPrinterMode;
  model: string | null;
  model_name: string | null;
  access_code_set: boolean;
  serial: string;
  target_printer_id: number | null;
  /** Library folder where files arriving via FTP land (m040). null = library root. */
  target_folder_id: number | null;
  auto_dispatch: boolean;
  /** When true (auto_queue mode), VP intake auto-pins per-slot type+color from each 3MF
   *  as `force_color_match` overrides so the eligibility scheduler refuses printers
   *  loaded with the right material in the wrong colour (#1188). */
  queue_force_color_match: boolean;
  bind_ip: string | null;
  remote_interface_ip: string | null;
  /** Tailscale per-VP cert provisioning (#1070) — defaults to true (off). */
  tailscale_disabled: boolean;
  position: number;
  status: {
    running: boolean;
    pending_files: number;
    tailscale_disabled?: boolean;
    proxy?: VirtualPrinterProxyStatus;
  };
}

/** Host-level Tailscale identity returned by `GET /virtual-printers/tailscale-status`
 *  (#1070 post-rip-out). Surfaces the IP + MagicDNS hostname users paste into the
 *  slicer when reaching the VP over Tailscale; cert-trust is unaffected. */
export interface TailscaleStatusResponse {
  available: boolean;
  hostname: string;
  tailnet_name: string;
  fqdn: string;
  tailscale_ips: string[];
  error: string | null;
}

export interface VirtualPrinterListResponse {
  printers: VirtualPrinterConfig[];
  models: Record<string, string>;
}

export const multiVirtualPrinterApi = {
  list: () => request<VirtualPrinterListResponse>('/virtual-printers'),

  get: (id: number) => request<VirtualPrinterConfig>(`/virtual-printers/${id}`),

  getTailscaleStatus: () =>
    request<TailscaleStatusResponse>('/virtual-printers/tailscale-status'),

  create: (data: {
    name?: string;
    enabled?: boolean;
    mode?: string;
    model?: string;
    access_code?: string;
    target_printer_id?: number;
    target_folder_id?: number;
    auto_dispatch?: boolean;
    queue_force_color_match?: boolean;
    bind_ip?: string;
    remote_interface_ip?: string;
    tailscale_disabled?: boolean;
  }) =>
    request<VirtualPrinterConfig>('/virtual-printers', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  update: (id: number, data: {
    name?: string;
    enabled?: boolean;
    mode?: string;
    model?: string;
    access_code?: string;
    target_printer_id?: number;
    /** Explicitly null out target_printer_id (Pydantic can't distinguish "absent" from "null"). */
    clear_target_printer?: boolean;
    target_folder_id?: number;
    /** Explicitly null out target_folder_id (m040). null = files land at library root. */
    clear_target_folder?: boolean;
    auto_dispatch?: boolean;
    queue_force_color_match?: boolean;
    bind_ip?: string;
    remote_interface_ip?: string;
    tailscale_disabled?: boolean;
  }) =>
    request<VirtualPrinterConfig>(`/virtual-printers/${id}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }),

  remove: (id: number) =>
    request<{ detail: string; id: number }>(`/virtual-printers/${id}`, {
      method: 'DELETE',
    }),
};

// Firmware API Types
export interface AvailableFirmwareVersion {
  version: string;
  file_available: boolean;
  download_url: string | null;
  release_notes: string | null;
  release_time: string | null;
}

export interface FirmwareUpdateInfo {
  printer_id: number;
  printer_name: string;
  model: string | null;
  current_version: string | null;
  latest_version: string | null;
  update_available: boolean;
  download_url: string | null;
  release_notes: string | null;
  available_versions: AvailableFirmwareVersion[];
}

export interface FirmwareUploadPrepare {
  can_proceed: boolean;
  sd_card_present: boolean;
  sd_card_free_space: number;
  firmware_size: number;
  space_sufficient: boolean;
  update_available: boolean;
  current_version: string | null;
  latest_version: string | null;
  target_version: string | null;
  firmware_filename: string | null;
  errors: string[];
}

export interface FirmwareUploadStatus {
  status: 'idle' | 'preparing' | 'downloading' | 'uploading' | 'complete' | 'error';
  progress: number;
  message: string;
  error: string | null;
  firmware_filename: string | null;
  firmware_version: string | null;
}

// Firmware API
export const firmwareApi = {
  checkUpdates: () =>
    request<{ updates: FirmwareUpdateInfo[]; updates_available: number }>('/firmware/updates'),

  checkPrinterUpdate: (printerId: number) =>
    request<FirmwareUpdateInfo>(`/firmware/updates/${printerId}`),

  prepareUpload: (printerId: number, version?: string) =>
    request<FirmwareUploadPrepare>(
      `/firmware/updates/${printerId}/prepare${version ? `?version=${encodeURIComponent(version)}` : ''}`,
    ),

  startUpload: (printerId: number, version?: string) =>
    request<{ started: boolean; message: string }>(
      `/firmware/updates/${printerId}/upload${version ? `?version=${encodeURIComponent(version)}` : ''}`,
      { method: 'POST' },
    ),

  getUploadStatus: (printerId: number) =>
    request<FirmwareUploadStatus>(`/firmware/updates/${printerId}/upload/status`),
};

// Support types
export interface DebugLoggingState {
  enabled: boolean;
  enabled_at: string | null;
  duration_seconds: number | null;
}

export interface LogEntry {
  timestamp: string;
  level: string;
  logger_name: string;
  message: string;
}

export interface LogsResponse {
  entries: LogEntry[];
  total_in_file: number;
  filtered_count: number;
}

// Support API
export const supportApi = {
  getDebugLoggingState: () =>
    request<DebugLoggingState>('/support/debug-logging'),

  setDebugLogging: (enabled: boolean) =>
    request<DebugLoggingState>('/support/debug-logging', {
      method: 'POST',
      body: JSON.stringify({ enabled }),
    }),

  downloadSupportBundle: async () => {
    const headers: Record<string, string> = {};
    if (authToken) {
      headers['Authorization'] = `Bearer ${authToken}`;
    }
    const response = await fetch(`${API_BASE}/support/bundle`, { headers });
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    // Get filename from Content-Disposition header or use default
    const disposition = response.headers.get('Content-Disposition');
    const filename = parseContentDispositionFilename(disposition) || 'bamdude-support.zip';

    // Download the blob
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },

  getLogs: (params?: { limit?: number; level?: string; search?: string }) => {
    const searchParams = new URLSearchParams();
    if (params?.limit) searchParams.set('limit', params.limit.toString());
    if (params?.level) searchParams.set('level', params.level);
    if (params?.search) searchParams.set('search', params.search);
    const query = searchParams.toString();
    return request<LogsResponse>(`/support/logs${query ? `?${query}` : ''}`);
  },

  clearLogs: () =>
    request<{ message: string }>('/support/logs', { method: 'DELETE' }),

  // Historical log archive management — populated by daily rotation.
  // Files matching ``bamdude-YYYY-MM-DD.log`` only; backend enforces
  // path-traversal guard.
  listLogArchives: () =>
    request<{ archives: { filename: string; size_bytes: number; mtime: string }[] }>(
      '/support/log-archives',
    ),

  downloadLogArchive: async (filename: string) => {
    const headers: Record<string, string> = {};
    if (authToken) headers['Authorization'] = `Bearer ${authToken}`;
    const response = await fetch(
      `${API_BASE}/support/log-archives/${encodeURIComponent(filename)}/download`,
      { headers },
    );
    if (!response.ok) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || `HTTP ${response.status}`);
    }
    const blob = await response.blob();
    const url = window.URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    window.URL.revokeObjectURL(url);
  },

  deleteLogArchive: (filename: string) =>
    request<{ message: string }>(
      `/support/log-archives/${encodeURIComponent(filename)}`,
      { method: 'DELETE' },
    ),
};

// Macros
export interface SwapProfile {
  id: string;
  label: string;
  description?: string | null;
  models: string[];
}

export type MacroActionType = 'gcode' | 'mqtt_action';

export interface MqttMacroAction {
  id: string;
  label: string;
  i18n_key: string;
}

export interface Macro {
  id: number;
  name: string;
  description: string | null;
  printer_models: string[];
  swap_mode_only: boolean;
  swap_profile: string | null;
  event: string;
  action_type: MacroActionType;
  mqtt_action: string | null;
  delay_seconds: number;
  gcode: string;
  is_custom: boolean;
  enabled: boolean;
  created_at: string;
  updated_at: string;
}

export interface MacroCreate {
  name: string;
  description?: string | null;
  printer_models: string[];
  swap_mode_only: boolean;
  swap_profile?: string | null;
  event: string;
  action_type?: MacroActionType;
  mqtt_action?: string | null;
  delay_seconds?: number;
  gcode: string;
  enabled: boolean;
}

export interface MacroUpdate {
  name?: string;
  description?: string | null;
  printer_models?: string[];
  swap_mode_only?: boolean;
  swap_profile?: string | null;
  event?: string;
  action_type?: MacroActionType;
  mqtt_action?: string | null;
  delay_seconds?: number;
  gcode?: string;
  enabled?: boolean;
}

export interface MacroMeta {
  events: Record<string, string>;
  // Events for which the "Swap profile" picker is relevant.
  swap_events: string[];
  printer_models: Record<string, string>;
  swap_profiles: SwapProfile[];
  mqtt_actions: MqttMacroAction[];
}

export interface MacroExecuteResponse {
  success: boolean;
  message: string;
  sequence_id: number | null;
}

// Bug Report API
export interface BugReportRequest {
  description: string;
  email?: string;
  screenshot_base64?: string;
  include_support_info?: boolean;
  debug_logs?: string;
}

export interface BugReportResponse {
  success: boolean;
  message: string;
  issue_url?: string;
  issue_number?: number;
}

export const bugReportApi = {
  submit: (data: BugReportRequest) =>
    request<BugReportResponse>('/bug-report/submit', {
      method: 'POST',
      body: JSON.stringify(data),
    }),
  startLogging: () =>
    request<{ started: boolean; was_debug: boolean }>('/bug-report/start-logging', {
      method: 'POST',
    }),
  stopLogging: (wasDebug: boolean) =>
    request<{ logs: string }>(`/bug-report/stop-logging?was_debug=${wasDebug}`, {
      method: 'POST',
    }),
};

// Macros API
export const macrosApi = {
  getMacros: () => request<Macro[]>('/macros/'),
  getMacroMeta: () => request<MacroMeta>('/macros/meta'),
  getSwapProfiles: () => request<SwapProfile[]>('/macros/swap-profiles'),
  createMacro: (data: MacroCreate) => request<Macro>('/macros/', { method: 'POST', body: JSON.stringify(data) }),
  updateMacro: (id: number, data: MacroUpdate) => request<Macro>('/macros/' + id, { method: 'PATCH', body: JSON.stringify(data) }),
  deleteMacro: (id: number) => request<{ message: string }>('/macros/' + id, { method: 'DELETE' }),
  executeMacro: (macroId: number, printerId: number) =>
    request<MacroExecuteResponse>(`/macros/${macroId}/execute?printer_id=${printerId}`, { method: 'POST' }),
};
