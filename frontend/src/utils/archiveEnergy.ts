import type { Archive } from '../api/client';

export type ArchiveEnergyFields = Pick<
  Archive,
  'energy_kwh' | 'energy_cost' | 'energy_status' | 'energy_coverage' | 'energy_reason'
>;

/** Whether an archive has an energy fact to show: a figure, or the statement that it has none
 *  (spec plug-meter-watchdog W11). */
export function hasEnergyFact(archive: ArchiveEnergyFields): boolean {
  return archive.energy_cost != null || archive.energy_status === 'unavailable';
}
