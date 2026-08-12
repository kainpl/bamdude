/**
 * `fetchPrinterCalibrations` — the fetch half of the PA-Profil nozzle fix (#2618).
 *
 * `getKProfiles` filters strictly by nozzle diameter and defaults to "0.4", so
 * the single call this replaces could only ever see 0.4 mm profiles. On a
 * dual-nozzle printer the 0.6 mm K value for the same filament was fetched by
 * nothing at all, and the picker said "1 match" where there were two — while
 * PAProfileSection's own multi-nozzle grouping sat there with nothing to group.
 */

import { describe, it, expect, vi, beforeEach } from 'vitest';

vi.mock('../../../api/client', () => ({
  api: { getKProfiles: vi.fn() },
}));

import { api } from '../../../api/client';
import { fetchPrinterCalibrations } from '../utils';

const getKProfiles = api.getKProfiles as unknown as ReturnType<typeof vi.fn>;

function profile(overrides: Record<string, unknown> = {}) {
  return {
    slot_id: 1,
    filament_id: 'GFN05',
    setting_id: '',
    name: 'PAHT-CF',
    k_value: '0.042',
    n_coef: '0',
    extruder_id: 0,
    nozzle_diameter: '0.4',
    ...overrides,
  };
}

beforeEach(() => {
  getKProfiles.mockReset();
});

describe('fetchPrinterCalibrations', () => {
  it('asks about every installed diameter and merges the answers', async () => {
    getKProfiles.mockImplementation((_id: number, d: string) =>
      Promise.resolve({
        profiles: [
          profile(
            d === '0.6'
              ? { slot_id: 11, k_value: '0.028', extruder_id: 1, nozzle_diameter: '0.6' }
              : {},
          ),
        ],
      }),
    );

    const result = await fetchPrinterCalibrations(7, {
      nozzles: [{ nozzle_diameter: '0.4' }, { nozzle_diameter: '0.6' }],
    });

    expect(getKProfiles).toHaveBeenCalledTimes(2);
    expect(getKProfiles).toHaveBeenCalledWith(7, '0.4');
    expect(getKProfiles).toHaveBeenCalledWith(7, '0.6');
    expect(result.map(c => c.k_value)).toEqual([0.042, 0.028]);
    expect(result.map(c => c.nozzle_diameter)).toEqual(['0.4', '0.6']);
  });

  it('falls back to 0.4 when the printer has not reported its nozzles', async () => {
    getKProfiles.mockResolvedValue({ profiles: [profile()] });

    const result = await fetchPrinterCalibrations(7, { nozzles: [] });

    // Exactly the previous behaviour — an unreported nozzle must not mean
    // "fetch nothing", which would empty the picker on every such printer.
    expect(getKProfiles).toHaveBeenCalledTimes(1);
    expect(getKProfiles).toHaveBeenCalledWith(7, '0.4');
    expect(result).toHaveLength(1);
  });

  it('asks once when both hotends carry the same diameter', async () => {
    getKProfiles.mockResolvedValue({ profiles: [profile()] });

    await fetchPrinterCalibrations(7, {
      nozzles: [{ nozzle_diameter: '0.4' }, { nozzle_diameter: '0.4' }],
    });

    expect(getKProfiles).toHaveBeenCalledTimes(1);
  });

  it('keeps the diameters that answered when one request fails', async () => {
    getKProfiles.mockImplementation((_id: number, d: string) =>
      d === '0.4'
        ? Promise.reject(new Error('printer does not support K-profiles'))
        : Promise.resolve({ profiles: [profile({ nozzle_diameter: '0.6' })] }),
    );

    const result = await fetchPrinterCalibrations(7, {
      nozzles: [{ nozzle_diameter: '0.4' }, { nozzle_diameter: '0.6' }],
    });

    // One unsupported diameter must not take the whole batch down with it —
    // that is what the per-caller try/catch used to guarantee.
    expect(result.map(c => c.nozzle_diameter)).toEqual(['0.6']);
  });

  it('coerces the string k/n values the API returns into numbers', async () => {
    getKProfiles.mockResolvedValue({
      profiles: [profile({ k_value: 'not-a-number', n_coef: '1.5', setting_id: undefined })],
    });

    const [cal] = await fetchPrinterCalibrations(7, { nozzles: [{ nozzle_diameter: '0.4' }] });

    expect(cal.k_value).toBe(0);
    expect(cal.n_coef).toBe(1.5);
    expect(cal.setting_id).toBe('');
  });
});
