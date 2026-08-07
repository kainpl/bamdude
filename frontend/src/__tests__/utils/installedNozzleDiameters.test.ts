/**
 * `installedNozzleDiameters` — which nozzles to ask the printer about (#2618).
 *
 * The PA-Profil picker must fetch K-profiles for every nozzle a printer really
 * has, not the hardcoded 0.4 mm default, or a 0.6 mm profile for the same
 * filament is never surfaced. This helper answers "which diameters", and the
 * contract that matters is its empty result: it means "the printer has not told
 * us", so the caller keeps its own fallback rather than fetching nothing.
 *
 * Mirrors the backend `print_scheduler._installed_nozzle_diameters`; the two are
 * meant to answer the same question the same way.
 */

import { describe, it, expect } from 'vitest';

import { installedNozzleDiameters } from '../../utils/amsHelpers';

describe('installedNozzleDiameters', () => {
  it('returns an empty array when there is no status at all', () => {
    expect(installedNozzleDiameters(null)).toEqual([]);
    expect(installedNozzleDiameters(undefined)).toEqual([]);
  });

  it('returns an empty array when no nozzles are reported', () => {
    expect(installedNozzleDiameters({ nozzles: [] })).toEqual([]);
    expect(installedNozzleDiameters({})).toEqual([]);
  });

  it('skips the empty-string and non-positive defaults a NozzleInfo starts with', () => {
    expect(
      installedNozzleDiameters({ nozzles: [{ nozzle_diameter: '' }, { nozzle_diameter: '0' }] }),
    ).toEqual([]);
  });

  it('returns the single installed diameter', () => {
    expect(installedNozzleDiameters({ nozzles: [{ nozzle_diameter: '0.4' }] })).toEqual(['0.4']);
  });

  it('returns both diameters on a dual-nozzle printer, in reported order', () => {
    expect(
      installedNozzleDiameters({ nozzles: [{ nozzle_diameter: '0.4' }, { nozzle_diameter: '0.6' }] }),
    ).toEqual(['0.4', '0.6']);
  });

  it('dedupes — two 0.4 hotends are one diameter to ask about', () => {
    expect(
      installedNozzleDiameters({ nozzles: [{ nozzle_diameter: '0.4' }, { nozzle_diameter: '0.4' }] }),
    ).toEqual(['0.4']);
  });

  it('keeps the valid diameter when the other hotend is still an empty default', () => {
    expect(
      installedNozzleDiameters({ nozzles: [{ nozzle_diameter: '0.6' }, { nozzle_diameter: '' }] }),
    ).toEqual(['0.6']);
  });

  it('trims whitespace rather than treating a padded value as junk', () => {
    expect(installedNozzleDiameters({ nozzles: [{ nozzle_diameter: ' 0.8 ' }] })).toEqual(['0.8']);
  });
});
