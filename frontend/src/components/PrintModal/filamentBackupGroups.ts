/**
 * AMS Filament Backup turns several trays into ONE pot of filament.
 *
 * With `auto_switch_filament` on, the printer swaps to another loaded tray of
 * the same filament and colour when the running one runs out — so a 200 g spool
 * and an 800 g spool of the same PLA cover a 300 g print between them, and the
 * pre-print check must weigh the pot, not each tray on its own. Bambu Studio
 * asks the same question the same way in
 * `SelectMachineDialog::CheckWarningFilamentRemain`, which decrements a local
 * remain map across the backup group rather than per tray.
 *
 * The grouping rule is the printer's own: same filament preset (`tray_info_idx`)
 * and same colour; where the preset is unknown, same type and colour. With
 * backup OFF every tray is its own group, which is exactly the strict per-tray
 * check this module replaced.
 */

import type { LoadedFilament } from '../../hooks/useFilamentMapping';

/**
 * One pot of interchangeable filament on a printer.
 *
 * `trayIds`/`labels` are parallel and follow the order the trays came out of
 * `buildLoadedFilaments`, so a message naming the group reads in AMS order.
 */
export type BackupGroup = {
  key: string;
  trayIds: number[];
  labels: string[];
};

/**
 * A nominal Bambu spool. The AMS reports a tray's fill as a PERCENTAGE and
 * never a weight — no Bambu AMS weighs anything — so a tray nobody registered
 * in inventory can only be converted to grams against an assumed reel size.
 * ⚠️ This is an assumption, not a measurement: 1 kg is what Bambu ships, and a
 * 250 g sample reel at 60 % would be over-counted four-fold. It is only ever
 * used to let an unregistered tray contribute to a backup pool — the registered
 * spool's own `label_weight` always wins when there is an assignment.
 */
export const NOMINAL_SPOOL_GRAMS = 1000;

/** The key of a group that contains exactly this tray and nothing else. */
export function privateBackupGroup(globalTrayId: number, label: string): BackupGroup {
  return { key: `tray:${globalTrayId}`, trayIds: [globalTrayId], labels: [label] };
}

/**
 * Which trays the printer would substitute for one another.
 *
 * @param loaded  every loaded tray, from `buildLoadedFilaments`
 * @param backupOn  the printer's `ams_auto_switch_filament`, and ONLY when it is
 *   literally `true` — `null` means "we could not read it", which is not consent
 *   to pool two spools together.
 * @returns globalTrayId → the group it belongs to. Trays that share a group
 *   share the same object, so `===` on the group (or on its `key`) answers
 *   "would the printer swap these two".
 */
export function groupTraysForBackup(loaded: LoadedFilament[], backupOn: boolean): Map<number, BackupGroup> {
  const byTray = new Map<number, BackupGroup>();

  if (!backupOn) {
    for (const tray of loaded) {
      byTray.set(tray.globalTrayId, privateBackupGroup(tray.globalTrayId, tray.label));
    }
    return byTray;
  }

  const byKey = new Map<string, BackupGroup>();
  for (const tray of loaded) {
    // The preset identifies the filament variant (GFA00 is PLA Basic, GFA01
    // PLA Matte), which is what the printer matches on; colour still has to
    // agree or the swap would change the part's colour mid-print. Trays with
    // no preset fall back to the type, which is all the AMS reported.
    const key = tray.trayInfoIdx ? `${tray.trayInfoIdx}|${tray.color}` : `${tray.type}|${tray.color}`;
    let group = byKey.get(key);
    if (!group) {
      group = { key, trayIds: [], labels: [] };
      byKey.set(key, group);
    }
    group.trayIds.push(tray.globalTrayId);
    group.labels.push(tray.label);
    byTray.set(tray.globalTrayId, group);
  }
  return byTray;
}

/**
 * Grams left on a tray whose only reading is the AMS fill percentage.
 *
 * ⚠️ `0` is refused, and that is deliberate: the firmware hands out `0` (and
 * `-1`) whenever it has nothing to say far more often than it measures an empty
 * spool. Treating it as "empty" is the same mistake the backend's
 * `utils/filament_remaining.grams_remaining` exists to avoid — see the vault
 * invariant `inv-zero-remain-is-not-an-empty-spool`. Unknown contributes
 * nothing to the pool, which is the conservative direction.
 *
 * @returns grams, or `null` when the reading is not a usable percentage.
 */
export function nominalGramsFromRemain(
  remain: number | undefined,
  nominalGrams: number = NOMINAL_SPOOL_GRAMS
): number | null {
  if (remain === undefined || !Number.isFinite(remain)) return null;
  if (remain <= 0 || remain > 100) return null;
  return (remain / 100) * nominalGrams;
}
