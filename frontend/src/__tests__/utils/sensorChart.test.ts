import { describe, expect, it } from 'vitest';

import { sensorBucketLabelKey, sensorChartPoints } from '../../utils/sensorChart';

describe('sensorChartPoints', () => {
  it('returns exactly the points it was given', () => {
    // HeaterHistoryModal duplicates its first and last point so the line reaches
    // the axis ends. On a 7-day window for a sensor adopted yesterday that draws
    // a week of flat line nobody measured.
    const points = [
      { recorded_at: '2026-08-04T10:00:00+00:00', value: 23.4 },
      { recorded_at: '2026-08-04T10:05:00+00:00', value: 23.9 },
    ];
    expect(sensorChartPoints(points)).toHaveLength(2);
  });

  it('turns the stamp into a number the axis can order', () => {
    const [point] = sensorChartPoints([{ recorded_at: '2026-08-04T10:00:00+00:00', value: 23.4 }]);
    expect(point.time).toBe(Date.parse('2026-08-04T10:00:00+00:00'));
    expect(point.value).toBe(23.4);
  });

  it('names a one-minute bucket differently from the rest', () => {
    // "23.4 °C" alone presents an average as an instant reading.
    expect(sensorBucketLabelKey(60)).not.toBe(sensorBucketLabelKey(300));
  });
});
