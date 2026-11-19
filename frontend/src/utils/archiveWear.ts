import type { Archive } from '../api/client';

export type ArchiveWearFields = Pick<Archive, 'wear_cost' | 'wear_rate'>;

/** Whether the server stamped a wear figure on this print. No figure is null, never a zero. */
export function hasWearFact(archive: ArchiveWearFields): boolean {
  return archive.wear_cost != null;
}
