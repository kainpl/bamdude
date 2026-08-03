import type { SensorMeasurement, ZigbeeSensor } from '../api/client';
import { compareLocationNames } from './locationOrder';
import type { LocationIndex } from './locationTree';

/** Quantities that describe the device, not the room. Shared with SensorCard so
 *  the two lists cannot drift into disagreeing about what a room reading is. */
export const DEVICE_KEYS = ['battery', 'battery_voltage'];

/** What this sensor says about the place it stands in. */
export function roomReadings(sensor: ZigbeeSensor): [string, SensorMeasurement][] {
  return Object.entries(sensor.measurements).filter(([key]) => !DEVICE_KEYS.includes(key));
}

/**
 * The sensors a location's group header shows: the ones bound to that location
 * or to any of its ancestors, nearest first.
 *
 * Ancestors only, never descendants. A sensor measures the place it stands in
 * and everything inside it — the workshop's air is also the air on its shelves —
 * but a shelf's reading is not the workshop's, and the reverse direction would
 * make the top-level group accumulate every sensor in the building.
 */
export function sensorsForGroup(
  sensors: ZigbeeSensor[],
  locationId: number | null,
  index: LocationIndex,
): ZigbeeSensor[] {
  // "No location" is not a place. Nothing measures it.
  if (locationId == null) return [];

  const chain = index.ancestorsOf(locationId);
  const rank = new Map(chain.map((id, position) => [id, position]));

  return sensors
    .filter((sensor) => {
      if (sensor.location == null || !rank.has(sensor.location.id)) return false;
      // A live sensor with nothing to say about the room gets no chip; one that
      // is off the mesh keeps its chip, because its measurements are empty
      // BECAUSE it is absent, and a dead sensor must not look like no sensor.
      return !sensor.present || roomReadings(sensor).length > 0;
    })
    .sort((a, b) => {
      const byDistance = rank.get(a.location!.id)! - rank.get(b.location!.id)!;
      // Two sensors in the same place would otherwise sit in whatever order the
      // API returned them, and the header would reshuffle between renders.
      return byDistance !== 0 ? byDistance : compareLocationNames(a.name, b.name);
    });
}
