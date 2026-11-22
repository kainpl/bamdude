/**
 * Units and names beside the Statistics figures are the UI language's.
 *
 * The page used to write "500.5h", "5.5kg", "1.500 kWh", "12am" and "Mon" into
 * a Ukrainian interface. The shared formatters take an optional translator
 * (the pattern `formatRelativeTime` already used); without one they keep the
 * English short form every other caller relies on.
 */
import { afterEach, describe, expect, it } from 'vitest';
import i18n from '../../i18n';
import { formatDuration, hourOfDayLabel, weekdayShortLabels } from '../../utils/date';
import { formatWeight } from '../../utils/weight';

const t = (key: string, options?: Record<string, unknown>) => i18n.t(key, options);
const NBSP = ' ';

afterEach(async () => {
  await i18n.changeLanguage('en');
});

describe('formatWeight', () => {
  it('keeps the English short form without a translator', () => {
    expect(formatWeight(512.4)).toBe('512g');
    expect(formatWeight(5500)).toBe('5.5kg');
    expect(formatWeight(2_000_000)).toBe('2t');
  });

  it('writes the same in English through the translator', () => {
    expect(formatWeight(512.4, t)).toBe('512g');
    expect(formatWeight(5500, t)).toBe('5.5kg');
    expect(formatWeight(2_000_000, t)).toBe('2t');
  });

  it('writes Ukrainian units, never parted from the number', async () => {
    await i18n.changeLanguage('uk');
    expect(formatWeight(512.4, t)).toBe(`512${NBSP}г`);
    expect(formatWeight(5500, t)).toBe(`5.5${NBSP}кг`);
    expect(formatWeight(2_000_000, t)).toBe(`2${NBSP}т`);
  });
});

describe('formatDuration', () => {
  it('keeps the English short form without a translator', () => {
    expect(formatDuration(28800)).toBe('8h 0m');
    expect(formatDuration(2700)).toBe('45m');
    expect(formatDuration(null)).toBe('--');
  });

  it('writes Ukrainian hours and minutes', async () => {
    await i18n.changeLanguage('uk');
    expect(formatDuration(30_600, t)).toBe(`8${NBSP}год 30${NBSP}хв`);
    expect(formatDuration(2700, t)).toBe(`45${NBSP}хв`);
    expect(formatDuration(undefined, t)).toBe('--');
  });
});

describe('hour and weekday names', () => {
  it('names an hour as the locale does', () => {
    expect(hourOfDayLabel(14, 'en')).toMatch(/^2\sPM$/);
    expect(hourOfDayLabel(0, 'en')).toMatch(/^12\sAM$/);
    expect(hourOfDayLabel(14, 'uk')).toBe('14');
  });

  it('lists the weekdays from the asked first day', () => {
    expect(weekdayShortLabels('en', 'monday')).toEqual(['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']);
    expect(weekdayShortLabels('en', 'sunday')).toEqual(['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']);
    const uk = weekdayShortLabels('uk', 'monday');
    expect(uk).toHaveLength(7);
    expect(uk[0].toLowerCase()).toMatch(/^пн/);
    expect(uk[6].toLowerCase()).toMatch(/^нд/);
  });
});
