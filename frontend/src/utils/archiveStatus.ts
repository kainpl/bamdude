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
 * True when the status represents a finished-unsuccessfully terminal state.
 * Used by calendar counts (failed tile + red dot).
 */
export function isFailureStatus(status: string | null | undefined): boolean {
  return status === 'failed' || status === 'aborted' || status === 'cancelled' || status === 'stopped';
}
