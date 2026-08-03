import type { PlugPowerPoint } from '../api/client';
import { parseUTCDate } from './date';

/**
 * API points as the chart wants them.
 *
 * Its own module rather than an export from the modal: the chart is mocked in
 * tests, so these two decisions would otherwise be unreachable — and the lint
 * rule against non-component exports from a component file is right that they
 * do not belong there.
 *
 * Two things are pinned here. A null stays a null: turning it into 0 would draw
 * the farm consuming nothing, which is a different claim from not knowing. And
 * the series is not padded out to the edges of the window — `HeaterHistoryModal`
 * duplicates its first and last point to reach the axis ends, and doing that
 * here would invent consumption across hours nobody recorded.
 */
export function chartPoints(points: PlugPowerPoint[]): { time: number; power: number | null }[] {
  return points.map((point) => ({
    time: (parseUTCDate(point.recorded_at) ?? new Date()).getTime(),
    power: point.power,
  }));
}

/** Which string names this bucket. A point is an average, and saying "72 W"
 *  alone presents it as an instant. */
export function bucketLabelKey(bucketSeconds: number): string {
  return bucketSeconds === 60 ? 'smartPlugs.powerHistory.bucketMinute' : 'smartPlugs.powerHistory.bucketMinutes';
}
