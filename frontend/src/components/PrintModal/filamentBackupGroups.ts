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
 * The grouping rule is the printer's own: same extruder, same filament preset
 * (`tray_info_idx`) and same colour; where the preset is unknown, same type and
 * colour. With backup OFF every tray is its own group, which is exactly the
 * strict per-tray check this module replaced.
 *
 * ⚠️ **Backup feeds from the AMS list only.** BS builds its remain map from the
 * AMS trays and takes the group from `GetBackupAmsSlotInGroup`, which is
 * extruder-scoped — the external spool holder is never a backup source, and no
 * swap crosses nozzles. So an external tray is always alone here whatever it
 * carries, and two AMS units bound to different extruders never pool.
 *
 * ⚠️ **A tray with no inventory spool contributes NOTHING to the pool.** It is
 * unknown, not empty — and the operator's rule is the simple one: register the
 * spool if it should count. The AMS reports a fill percentage, never a weight,
 * and only for RFID spools at that, so guessing grams from it against an
 * assumed reel size was never a general answer — it would have suppressed real
 * warnings on the strength of a number nobody measured.
 *
 * ⚠️ **One deliberate divergence from BS**: it gates the whole check on the
 * printer supporting accurate remain reporting, where we gate on
 * `ams_auto_switch_filament` alone. Neither of us guesses a reel size.
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
    // ⚠️ The external holder is not a backup source — the printer never feeds a
    // swap from it — so it is alone even when its preset and colour match an
    // AMS tray exactly. `buildLoadedFilaments` appends `vt_tray` entries with a
    // real `trayInfoIdx` and the same normalised colour, so without this they
    // would join an AMS group and their weight would suppress a real warning.
    if (tray.isExternal) {
      byTray.set(tray.globalTrayId, privateBackupGroup(tray.globalTrayId, tray.label));
      continue;
    }
    // The extruder leads the key: no swap crosses nozzles, so two AMS units
    // bound to different extruders on dual-nozzle hardware are two pots, not
    // one. `undefined` (single-nozzle, no `ams_extruder_map`) is its own bucket
    // and groups with itself, which is the single-nozzle case.
    //
    // Then the preset, which identifies the filament variant (GFA00 is PLA
    // Basic, GFA01 PLA Matte) and is what the printer matches on; colour still
    // has to agree or the swap would change the part's colour mid-print. Trays
    // with no preset fall back to the type, tagged so a preset that happens to
    // read like a type name cannot collide with it.
    const filament = tray.trayInfoIdx ? tray.trayInfoIdx : `type:${tray.type}`;
    const key = `${tray.extruderId ?? 'x'}|${filament}|${tray.color}`;
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
