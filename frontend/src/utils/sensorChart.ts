import type { SensorHistoryPoint } from '../api/client';
import { parseUTCDate } from './date';

/**
 * API points as the chart wants them.
 *
 * Its own module rather than an export from the modal: recharts is mocked in
 * tests, so this would otherwise be unreachable — and the lint rule against
 * non-component exports from a component file is right that it does not belong
 * there.
 *
 * What is pinned here is an absence. `HeaterHistoryModal` duplicates its first
 * and last point so the line reaches the ends of the axis; doing that here would
 * draw a week of flat line for a sensor adopted yesterday.
 */
export function sensorChartPoints(points: SensorHistoryPoint[]): { time: number; value: number }[] {
  return points.map((point) => ({
    time: (parseUTCDate(point.recorded_at) ?? new Date()).getTime(),
    value: point.value,
  }));
}

/** Which string names this bucket. A point is an average, and "23.4 °C" alone
 *  presents it as an instant reading. */
export function sensorBucketLabelKey(bucketSeconds: number): string {
  return bucketSeconds === 60 ? 'sensorHistory.bucketMinute' : 'sensorHistory.bucketMinutes';
}
