/**
 * Central mapping from a PrintArchive.status string to the UI badge shape.
 *
 * Keeps ArchivesPage (grid + list) and CalendarView in sync — they all pull
 * the same label / color / pulse treatment. Returns null when no badge should
 * show (i.e. the "completed" happy path).
 */

export type ArchiveStatusBadge = {
  labelKey: string;
  // Tailwind classes for the badge background + text color.
  className: string;
  // Whether the badge should pulse (for "live"/in-progress states).
  pulse: boolean;
};

export function getArchiveStatusBadge(status: string | null | undefined): ArchiveStatusBadge | null {
  switch (status) {
    case 'printing':
      return {
        labelKey: 'archives.card.printing',
        className: 'bg-bambu-blue/80 text-white',
        pulse: true,
      };
    case 'archived':
      return {
        labelKey: 'archives.card.archived',
        className: 'bg-bambu-gray/80 text-white',
        pulse: false,
      };
    case 'failed':
      return {
        labelKey: 'archives.card.failed',
        className: 'bg-status-error/80 text-white',
        pulse: false,
      };
    case 'aborted':
      return {
        labelKey: 'archives.card.aborted',
        className: 'bg-status-error/80 text-white',
        pulse: false,
      };
    case 'cancelled':
      return {
        labelKey: 'archives.card.cancelled',
        className: 'bg-status-error/80 text-white',
        pulse: false,
      };
    case 'stopped':
      return {
        labelKey: 'archives.card.stopped',
        className: 'bg-status-error/80 text-white',
        pulse: false,
      };
    case 'completed':
    default:
      // Success: no badge clutter — the card already visualises it.
      return null;
  }
}

/**
 * What a print's status may be set to by hand, in the order the editor offers
 * them. Mirrors the server's `HAND_SET_STATUSES` (schemas/archive.py) minus
 * `stopped`, which no writer produces any more: it is shown when stored, never
 * offered. `printing` belongs to the live print — never a target, and a running
 * print's status is not edited at all.
 */
export const HAND_SET_ARCHIVE_STATUSES = ['completed', 'failed', 'aborted', 'cancelled'] as const;

/** Every status the backend has ever stored, each with a name of its own. */
const NAMED_ARCHIVE_STATUSES: ReadonlySet<string> = new Set([
  ...HAND_SET_ARCHIVE_STATUSES,
  'stopped',
  'printing',
  'archived',
]);

/**
 * The one source of a status's NAME (the editor's options, anywhere a status is
 * spelled out): `archives.statusNames.*`. A value outside the vocabulary is shown as
 * itself rather than as some other status.
 */
export function archiveStatusLabel(
  t: (key: string) => string,
  status: string | null | undefined,
): string {
  if (!status) return '';
  return NAMED_ARCHIVE_STATUSES.has(status) ? t(`archives.statusNames.${status}`) : status;
}

/**
 * True when the status represents a finished-unsuccessfully terminal state.
 * Used by calendar counts (failed tile + red dot).
 */
export function isFailureStatus(status: string | null | undefined): boolean {
  return status === 'failed' || status === 'aborted' || status === 'cancelled' || status === 'stopped';
}
