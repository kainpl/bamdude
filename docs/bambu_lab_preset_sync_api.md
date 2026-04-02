# Bambu Lab Preset Sync API Documentation

This document describes the Bambu Lab cloud API endpoints for syncing slicer presets (filament, print process, and machine profiles) between Bambu Studio and the cloud.

**Captured from:** Bambu Studio v2.4.0.70 with bambu_network_agent v02.04.00.58
**Date:** 2025-12-08

---

## Authentication

All API requests require authentication via Bearer token.

### Required Headers

```http
Host: api.bambulab.com
Authorization: Bearer <access_token>
User-Agent: bambu_network_agent/02.04.00.58
X-BBL-Client-Name: BambuStudio
X-BBL-Client-Type: slicer
X-BBL-Client-Version: 02.04.00.70
X-BBL-Device-ID: <uuid>
X-BBL-Language: en-US
X-BBL-OS-Type: macos|windows|linux
X-BBL-OS-Version: <version>
X-BBL-Agent-Version: 02.04.00.58
accept: application/json
```

---

## Endpoints

### 1. Get User Profile

```http
GET /v1/user-service/my/profile
```

Returns user account information including UID.

### 2. List All User Presets

```http
GET /v1/iot-service/api/slicer/setting?version={slicer_version}&public=false
```

**Parameters:**
- `version`: Slicer version (e.g., `2.4.0.5`)
- `public`: Set to `false` for user presets only

**Response:** Returns a list of preset IDs that the user has synced to cloud.

### 3. Get Individual Preset

```http
GET /v1/iot-service/api/slicer/setting/{preset_id}
```

**Response:**
```json
{
    "message": "success",
    "code": null,
    "error": null,
    "public": false,
    "version": "1.5.0.20",
    "type": "filament",
    "name": "Devil Design PLA @Bambu Lab X1 Carbon 0.6 nozzle",
    "update_time": "2025-12-08 01:06:27",
    "nickname": null,
    "base_id": "GFSA00",
    "setting": {
        "inherits": "Bambu PLA Basic @BBL X1C",
        "filament_vendor": "\"Devil Design\"",
        "nozzle_temperature": "225,220",
        "pressure_advance": "0.03",
        "updated_time": "1765138658"
    },
    "filament_id": null
}
```

---

## Preset ID Naming Convention

Preset IDs follow a specific prefix pattern indicating the type:

| Prefix | Type | Description |
|--------|------|-------------|
| `PPUS` | Print Process | Print/quality settings (layer height, speeds, infill, etc.) |
| `PFUS` | Filament | Filament settings (temperatures, flow, pressure advance, etc.) |
| `PMUS` | Printer/Machine | Machine settings (gcode, bed size, kinematics, etc.) |

The suffix after the prefix is a unique hash identifier.

**Examples:**
- `PPUS1b03400426f57d` - Print process preset
- `PFUS169056f3003bb4` - Filament preset
- `PMUSbc396893c54df0` - Machine/printer preset

---

## Preset Response Schema

### Common Fields

| Field | Type | Description |
|-------|------|-------------|
| `message` | string | API response status ("success") |
| `code` | int/null | Error code if any |
| `error` | string/null | Error message if any |
| `public` | boolean | Whether preset is publicly shared |
| `version` | string | Preset version |
| `type` | string | Preset type: "filament", "print", or "printer" |
| `name` | string | Display name of the preset |
| `update_time` | string | Last update timestamp (ISO format) |
| `nickname` | string/null | Optional user-defined nickname |
| `base_id` | string | Reference ID of the parent/base preset |
| `setting` | object | Key-value pairs of customized settings |
| `filament_id` | string/null | Bambu filament ID if applicable |

### Setting Object

The `setting` object contains **only the delta/modified values** from the parent preset. Key fields include:

- `inherits`: Name of the parent preset this inherits from
- `updated_time`: Unix timestamp of last modification
- Other fields depend on preset type (see below)

---

## Preset Types and Common Settings

### Filament Presets (PFUS)

```json
{
    "inherits": "Bambu PLA Basic @BBL X1C",
    "filament_vendor": "\"Devil Design\"",
    "filament_cost": "20",
    "filament_settings_id": "\"Devil Design PLA @Bambu Lab X1 Carbon 0.6 nozzle\"",
    "nozzle_temperature": "225,220",
    "nozzle_temperature_initial_layer": "225,220",
    "hot_plate_temp": "60",
    "cool_plate_temp": "60",
    "textured_plate_temp": "60",
    "pressure_advance": "0.03",
    "enable_pressure_advance": "1",
    "filament_max_volumetric_speed": "30,29",
    "activate_air_filtration": "1",
    "during_print_exhaust_fan_speed": "50",
    "complete_print_exhaust_fan_speed": "50",
    "close_fan_the_first_x_layers": "2",
    "overhang_fan_threshold": "10%",
    "slow_down_layer_time": "5",
    "temperature_vitrification": "65",
    "filament_start_gcode": "...",
    "filament_end_gcode": "..."
}
```

### Print Process Presets (PPUS)

```json
{
    "inherits": "0.08mm Extra Fine @BBL H2D",
    "print_settings_id": "# 0.08mm Extra Fine @BBL H2D",
    "prime_tower_max_speed": "100",
    "prime_tower_rib_wall": "0",
    "prime_tower_width": "20"
}
```

### Machine/Printer Presets (PMUS)

```json
{
    "inherits": "Bambu Lab H2D 0.4 nozzle",
    "printer_settings_id": "# Bambu Lab H2D 0.4 nozzle",
    "bed_custom_model": "/path/to/model.stl",
    "machine_start_gcode": "...",
    "machine_end_gcode": "...",
    "change_filament_gcode": "...",
    "printer_notes": "...",
    "support_air_filtration": "1"
}
```

---

## Base ID Reference

The `base_id` field references Bambu's internal preset database:

| Prefix | Type |
|--------|------|
| `GF` | Generic Filament |
| `GP` | Generic Print Process |
| `GM` | Generic Machine |

Examples:
- `GFSA00` - Generic filament base
- `GP136` - Generic print process base
- `GM033` - Generic machine base (H2D)

---

## API Operations (Verified)

### Create Preset

```http
POST /v1/iot-service/api/slicer/setting
Content-Type: application/json

{
    "type": "filament",
    "name": "My Custom PLA",
    "version": "2.0.0.0",
    "base_id": "GFSA00",
    "setting": {
        "inherits": "Bambu PLA Basic @BBL X1C",
        "nozzle_temperature": "210,205",
        "updated_time": "1733665800"
    }
}
```

**Required fields:**
- `type`: "filament", "print", or "printer"
- `name`: Display name
- `version`: Version string (e.g., "2.0.0.0", "2.3.0.2")
- `base_id`: Parent preset ID
- `setting`: Object with modified values including `updated_time` (Unix timestamp)

**Response:**
```json
{
    "message": "success",
    "code": null,
    "error": null,
    "setting_id": "PFUSe99f2ff04974b4",
    "update_time": "2025-12-08 16:31:48"
}
```

### Update Preset

**Important:** The Bambu Cloud API does NOT support true updates via PUT/PATCH.

- `PUT /v1/iot-service/api/slicer/setting/{preset_id}` returns **405 Method Not Allowed**
- `PATCH /v1/iot-service/api/slicer/setting/{preset_id}` returns **500 Cloud database failed**

**Workaround:** To "update" a preset:
1. GET the existing preset details
2. Merge your changes
3. POST to create a new preset (returns new `setting_id`)
4. DELETE the old preset

This mimics how Bambu Studio handles preset updates.

### Delete Preset

```http
DELETE /v1/iot-service/api/slicer/setting/{preset_id}
```

**Response:**
```json
{
    "message": "success",
    "code": null,
    "error": null
}
```

---

## Related Endpoints

### Slicer Resources

```http
GET /v1/iot-service/api/slicer/resource?slicer/plugins/cloud={version}
GET /v1/iot-service/api/slicer/resource?slicer/printer/bbl={version}
GET /v1/iot-service/api/slicer/resource?policy/privacy={version}
```

### User Print Status

```http
GET /v1/iot-service/api/user/print?force=true
```

### User Tasks

```http
GET /v1/user-service/my/tasks?limit=5&offset=0&status=0
```

### MQTT Certificate

```http
GET /v1/iot-service/api/user/applications/{app_id}/cert?aes256={encrypted_key}
```

---

## Notes

1. **Delta Storage**: Presets only store modified values from the parent, using the `inherits` field to reference the base preset.

2. **Version Tracking**: The `updated_time` field (Unix timestamp) is used for sync conflict resolution.

3. **Gcode Escaping**: Gcode fields use `\\n` for newlines within JSON strings.

4. **Multi-value Fields**: Some fields like `nozzle_temperature` contain comma-separated values for different conditions.

5. **Authentication**: The access token can be obtained via Bambu Lab OAuth flow or the `/v1/user-service/user/ticket/{code}` endpoint.
