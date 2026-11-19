/**
 * Statistics presets are LOCAL calendar days.
 *
 * The server reads `date_from` / `date_to` as days in the zone this browser
 * sends with every request. Built from the UTC date they were a day behind for
 * the first hours after local midnight east of UTC: «Today» showed yesterday,
 * and a week, a month and a year opened on the wrong day.
 */

import { describe, it, expect, beforeAll, afterAll, afterEach, vi } from 'vitest';
import { computeDateRange, localToday } from '../../utils/statsDateRange';

describe('computeDateRange — 01:30 local, east of UTC', () => {
  const saved = process.env.TZ;
  beforeAll(() => {
    // UTC+2 in winter, UTC+3 in summer.
    process.env.TZ = 'Europe/Kyiv';
  });
  afterAll(() => {
    process.env.TZ = saved;
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** 01:30 local on the given day — still the previous day in UTC. */
  function at(year: number, month: number, day: number): Date {
    const now = new Date(year, month - 1, day, 1, 30);
    vi.useFakeTimers();
    vi.setSystemTime(now);
    return now;
  }

  it('is a zone where the UTC date is still yesterday (the test means something)', () => {
    const now = at(2026, 11, 19);
    expect(now.toISOString().split('T')[0]).toBe('2026-11-18');
  });

  it('«Today» is the local day', () => {
    at(2026, 11, 19);
    expect(computeDateRange('today')).toEqual({ dateFrom: '2026-11-19', dateTo: '2026-11-19' });
    expect(localToday()).toBe('2026-11-19');
  });

  it('«This week» starts on this week’s Monday, on a Monday night too', () => {
    at(2026, 11, 16); // a Monday
    expect(computeDateRange('this-week')).toEqual({ dateFrom: '2026-11-16', dateTo: '2026-11-16' });
    at(2026, 11, 22); // the Sunday that ends it
    expect(computeDateRange('this-week')).toEqual({ dateFrom: '2026-11-16', dateTo: '2026-11-22' });
  });

  it('«This month» on the 1st is the new month', () => {
    at(2026, 12, 1);
    expect(computeDateRange('this-month')).toEqual({ dateFrom: '2026-12-01', dateTo: '2026-12-01' });
  });

  it('«This year» on 1 January is the new year', () => {
    at(2027, 1, 1);
    expect(computeDateRange('this-year')).toEqual({ dateFrom: '2027-01-01', dateTo: '2027-01-01' });
  });

  it('rolling windows end today and cross a month and a daylight-saving change', () => {
    at(2026, 11, 3);
    expect(computeDateRange('last-7')).toEqual({ dateFrom: '2026-10-28', dateTo: '2026-11-03' });
    expect(computeDateRange('last-30')).toEqual({ dateFrom: '2026-10-05', dateTo: '2026-11-03' });
    expect(computeDateRange('last-90')).toEqual({ dateFrom: '2026-08-06', dateTo: '2026-11-03' });
  });

  it('summer time (UTC+3) reads the same way', () => {
    const now = at(2026, 7, 1);
    expect(now.toISOString().split('T')[0]).toBe('2026-06-30');
    expect(computeDateRange('today')).toEqual({ dateFrom: '2026-07-01', dateTo: '2026-07-01' });
    expect(computeDateRange('this-month')).toEqual({ dateFrom: '2026-07-01', dateTo: '2026-07-01' });
  });

  it('«All time» and «Custom» name no dates', () => {
    at(2026, 11, 19);
    expect(computeDateRange('all-time')).toEqual({ dateFrom: undefined, dateTo: undefined });
    expect(computeDateRange('custom')).toEqual({});
  });
});
