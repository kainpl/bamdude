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
 * A reading as a person reads it: tenths at most.
 *
 * Every quantity arrives as `raw * scale` — hundredths of a degree times 0.01 —
 * and binary floating point turns that into 23.400000000000002 often enough to
 * be seen. A tenth is also all these sensors resolve to, so nothing is lost.
 *
 * A trailing zero is dropped: a limit of 41 should not read as 41.0, which
 * looks like a measurement rather than a round number. Used for the axis ticks
 * too, where recharts picks the values itself and picks them badly.
 */
export function formatReading(value: number | null | undefined): string {
  if (value == null || Number.isNaN(value)) return '—';
  return String(Number(value.toFixed(1)));
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

/**
 * The sensors a printer's card shows: the ones bound to that machine.
 *
 * No ancestor walk and no location fallback. A sensor bound to a place belongs
 * to the place — it already appears on that group's header, and repeating it on
 * every card standing in the room would say the enclosure reads what the room
 * reads. Binding to the printer is the operator saying otherwise, and it is the
 * only thing that puts a sensor here.
 */
export function sensorsForPrinter(sensors: ZigbeeSensor[], printerId: number): ZigbeeSensor[] {
  return sensors
    .filter((sensor) => sensor.printer_id === printerId)
    // Same rule as the group header: a live sensor with nothing to say gets no
    // chip, an absent one keeps its chip, because its readings are empty
    // BECAUSE it is absent and a dead sensor must not look like no sensor.
    .filter((sensor) => !sensor.present || roomReadings(sensor).length > 0)
    .sort((a, b) => compareLocationNames(a.name, b.name));
}
