import {
  buildFilamentComparison,
  type FilamentRequirement,
  type LoadedFilament,
} from '../hooks/useFilamentMapping';

export interface EligibilityInput {
  /** What this plate needs, as the dialog would receive it. */
  requirements: FilamentRequirement[];
  loadedFilaments: LoadedFilament[];
  /** How many printers the run targets. 0 = auto-queue. */
  printerCount: number;
  ftsActive?: boolean;
  trayNow?: number;
}

export type EligibilityVerdict = { ok: true } | { ok: false; reason: 'filament_type' };

/**
 * Whether this plate can join a silent run, or has to be asked about.
 *
 * ⚠️ **Reuses `buildFilamentComparison` rather than re-deciding.** Its
 * `FilamentStatus` vocabulary already names exactly the operator's rule:
 * `match` and `type_only` both mean "we found a spool" — `type_only` IS the
 * colour-mismatch case — while `mismatch` means no free spool of that type
 * exists. A second matcher here would drift from the one the visible dialog
 * and the dispatcher use, and the drift would show up as a silent print on the
 * wrong reel.
 *
 * ⚠️ **Skipped for a multi-printer or auto-queue run.** There the items ship
 * with no mapping and the scheduler computes one per plate when it picks the
 * printer, so the visible dialog would have had nothing to ask either. We
 * interrupt only where a dialog would genuinely have a question.
 *
 * `ftsActive` / `trayNow` are threaded through so the comparison run here is
 * the identical call the dialog makes for the same plate — `ftsActive` in
 * particular lifts the per-nozzle slot restriction and can therefore turn a
 * `mismatch` into a match.
 *
 * The two arguments the dialog passes and this does not are deliberate, and
 * neither can move the verdict: manual overrides (`{}`) because a silent run
 * has no panel to override a slot in, and `prefer_lowest_filament` because it
 * only ever reorders candidates *within* a match tier — it can change which
 * tray is picked, never whether one was found.
 */
export function canQueueWithoutAsking({
  requirements,
  loadedFilaments,
  printerCount,
  ftsActive = false,
  trayNow,
}: EligibilityInput): EligibilityVerdict {
  if (printerCount !== 1) return { ok: true };
  if (requirements.length === 0) return { ok: true };

  const comparison = buildFilamentComparison(
    { filaments: requirements },
    loadedFilaments,
    {},
    ftsActive,
    trayNow
  );
  const missingType = comparison.some((c) => c.status === 'mismatch');
  return missingType ? { ok: false, reason: 'filament_type' } : { ok: true };
}
