import { describe, expect, it } from 'vitest';

import { buildLocationIndex } from '../../utils/locationTree';
import type { LocationNode } from '../../utils/locationTree';
import { formatReading, roomReadings, sensorsForGroup, sensorsForPrinter } from '../../utils/sensorReadings';
import type { ZigbeeSensor } from '../../api/client';

// Workshop(1) -> Shelf 1(2) -> Box(4);  Workshop -> Shelf 2(3);  Hall(5)
const ROWS: LocationNode[] = [
  { id: 1, name: 'Workshop', parent_id: null, path: 'Workshop', depth: 1 },
  { id: 2, name: 'Shelf 1', parent_id: 1, path: 'Workshop / Shelf 1', depth: 2 },
  { id: 3, name: 'Shelf 2', parent_id: 1, path: 'Workshop / Shelf 2', depth: 2 },
  { id: 4, name: 'Box', parent_id: 2, path: 'Workshop / Shelf 1 / Box', depth: 3 },
  { id: 5, name: 'Hall', parent_id: null, path: 'Hall', depth: 1 },
];

const INDEX = buildLocationIndex(ROWS);

function reading(value: number, unit: string) {
  return {
    value,
    unit,
    last_report_at: '2026-08-04T10:00:00+00:00',
    stale: false,
    reporting: 'ok',
    verification: 'verified',
  };
}

function sensor(over: Partial<ZigbeeSensor> = {}): ZigbeeSensor {
  return {
    id: 1,
    name: 'S',
    location: null,
    printer_id: null,
    printer_name: null,
    ieee: 'aa:bb',
    nwk: 1,
    manufacturer: 'SONOFF',
    model: 'SNZB-02DR2',
    power: 'battery',
    quirk_applied: true,
    unreachable: false,
    present: true,
    measurements: { temperature: reading(23.4, '°C') },
    ...over,
  };
}

function at(id: number, path: string, over: Partial<ZigbeeSensor> = {}) {
  return sensor({ location: { id, name: path.split(' / ').pop()!, parent_id: null, path }, ...over });
}

describe('roomReadings', () => {
  it('drops the quantities that describe the device, not the room', () => {
    const s = sensor({
      measurements: {
        temperature: reading(23.4, '°C'),
        battery: reading(88, '%'),
        battery_voltage: reading(3.1, 'V'),
      },
    });
    expect(roomReadings(s).map(([key]) => key)).toEqual(['temperature']);
  });
});

describe('sensorsForGroup', () => {
  it('shows an ancestor sensor in a descendant group', () => {
    // The whole point of the hierarchy: one sensor on the workshop covers every
    // shelf without one sensor per shelf.
    const workshop = at(1, 'Workshop');
    expect(sensorsForGroup([workshop], 4, INDEX)).toEqual([workshop]);
  });

  it('does not show a descendant sensor in an ancestor group', () => {
    // A shelf's reading is not the workshop's, and the top group would otherwise
    // accumulate every sensor in the building.
    expect(sensorsForGroup([at(2, 'Workshop / Shelf 1')], 1, INDEX)).toEqual([]);
  });

  it('does not cross into a sibling branch', () => {
    expect(sensorsForGroup([at(3, 'Workshop / Shelf 2')], 2, INDEX)).toEqual([]);
  });

  it('orders the nearest sensor first', () => {
    const far = at(1, 'Workshop', { id: 10 });
    const near = at(2, 'Workshop / Shelf 1', { id: 11 });
    expect(sensorsForGroup([far, near], 2, INDEX).map((s) => s.id)).toEqual([11, 10]);
  });

  it('breaks a tie at the same level by name, so the header does not reshuffle', () => {
    const b = at(1, 'Workshop', { id: 20, name: 'Бокс' });
    const a = at(1, 'Workshop', { id: 21, name: 'Ангар' });
    expect(sensorsForGroup([b, a], 1, INDEX).map((s) => s.id)).toEqual([21, 20]);
  });

  it('gives the ungrouped group nothing', () => {
    // "No location" is not a place, so nothing measures it.
    expect(sensorsForGroup([at(1, 'Workshop')], null, INDEX)).toEqual([]);
  });

  it('skips a live sensor that says nothing about the room', () => {
    // A battery reading is about the device. A chip with nothing in it is noise.
    const batteryOnly = at(1, 'Workshop', { measurements: { battery: reading(88, '%') } });
    expect(sensorsForGroup([batteryOnly], 1, INDEX)).toEqual([]);
  });

  it('keeps a sensor that is not on the mesh, though it has no readings at all', () => {
    // Its measurements are empty BECAUSE it is absent — the backend derives the
    // quantity list from a live device's clusters. Dropping it would make a dead
    // sensor and no sensor look identical, which is the news worth showing.
    const gone = at(1, 'Workshop', { present: false, measurements: {} });
    expect(sensorsForGroup([gone], 1, INDEX)).toEqual([gone]);
  });
});

describe('formatReading', () => {
  it('trims the float noise a conversion leaves behind', () => {
    // Hundredths of a degree times 0.01. This is what the screen was showing.
    expect(formatReading(23.400000000000002)).toBe('23.4');
    expect(formatReading(41.199999999999996)).toBe('41.2');
  });

  it('keeps tenths and rounds past them', () => {
    expect(formatReading(23.46)).toBe('23.5');
    expect(formatReading(23.44)).toBe('23.4');
    expect(formatReading(-5.04)).toBe('-5');
  });

  it('does not promise anything about an exact midpoint', () => {
    // 23.45 is not 23.45 in binary — it is a hair below, so it rounds down
    // here and would with Math.round too. Getting 23.5 needs decimal
    // arithmetic, which is not worth carrying to draw a thermometer.
    expect(formatReading(23.45)).toBe('23.4');
  });

  it('drops a trailing zero', () => {
    // "41.0" reads as a measurement; 41 reads as the round number it is.
    expect(formatReading(41)).toBe('41');
    expect(formatReading(41.0001)).toBe('41');
  });

  it('says nothing rather than zero when there is no reading', () => {
    // A fabricated 0 is worse than a missing one — the rule plug power follows.
    expect(formatReading(null)).toBe('—');
    expect(formatReading(undefined)).toBe('—');
    expect(formatReading(Number.NaN)).toBe('—');
  });
});

describe('sensorsForPrinter', () => {
  it('takes the ones bound to that printer', () => {
    const mine = sensor({ id: 1, printer_id: 7 });
    const theirs = sensor({ id: 2, printer_id: 8 });

    expect(sensorsForPrinter([mine, theirs], 7)).toEqual([mine]);
  });

  it('ignores a sensor bound to the place the printer stands in', () => {
    // ⚠️ Deliberate, and the difference from the group header: a place sensor
    // already shows there. Repeating it on every card in the room would claim
    // the enclosure reads what the room reads.
    const roomProbe = sensor({ id: 3, location: { id: 1, name: 'Workshop', parent_id: null } as never });

    expect(sensorsForPrinter([roomProbe], 7)).toEqual([]);
  });

  it('drops a live sensor with nothing to say about its surroundings', () => {
    const batteryOnly = sensor({ id: 4, printer_id: 7, measurements: { battery: reading(88, '%') } });

    expect(sensorsForPrinter([batteryOnly], 7)).toEqual([]);
  });

  it('keeps one that is off the mesh', () => {
    // Its readings are empty BECAUSE it is absent; a dead sensor must not look
    // like no sensor.
    const gone = sensor({ id: 5, printer_id: 7, present: false, measurements: {} });

    expect(sensorsForPrinter([gone], 7)).toEqual([gone]);
  });

  it('orders by name so the card does not reshuffle between renders', () => {
    const b = sensor({ id: 6, name: 'Bed', printer_id: 7 });
    const a = sensor({ id: 7, name: 'Ambient', printer_id: 7 });

    expect(sensorsForPrinter([b, a], 7).map((s) => s.name)).toEqual(['Ambient', 'Bed']);
  });
});
