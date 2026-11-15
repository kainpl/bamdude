/**
 * Which inventory answers for a printer slot.
 *
 * The switch to Spoolman empties the built-in slot ledger, and the backend
 * refuses a built-in assignment while Spoolman mode is on. So the built-in
 * ledger is consulted only in `builtin` mode — never as a fallback while
 * Spoolman is merely unreachable, which is how a built-in row came to be
 * written and shown in Spoolman mode (upstream 62fad59fd, issue #3430).
 *
 * A status that has not answered yet (loading, or the request failed) is
 * `unknown`, not `builtin`: reading it as built-in offered the built-in Assign
 * dialog on a Spoolman install, where the backend refuses it.
 */
export type SlotInventoryMode = 'builtin' | 'spoolman' | 'spoolman-unreachable' | 'unknown';

export function slotInventoryMode(
  status: { enabled?: boolean; connected?: boolean } | null | undefined,
): SlotInventoryMode {
  if (!status) return 'unknown';
  if (!status.enabled) return 'builtin';
  return status.connected ? 'spoolman' : 'spoolman-unreachable';
}

/** The built-in slot rows a view may use: none at all while Spoolman is the inventory. */
export function builtinAssignments<T>(spoolmanMode: boolean, rows: T[] | undefined): T[] | undefined {
  return spoolmanMode ? undefined : rows;
}
