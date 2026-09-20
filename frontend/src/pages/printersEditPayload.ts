import type { BackupCompatibilityPolicy } from '../api/client';

/**
 * Normalises what the Edit Printer form holds for the backup-compatibility policy into what PATCH /printers/{id} accepts.
 *
 * The ONE normaliser on this side: the colour input stores its raw `#rrggbb`
 * and the server may have persisted `RRGGBBFF`, so both shapes arrive here and
 * exactly one opaque 8-digit colour leaves. A three-digit shorthand (`#abc`) is
 * deliberately NOT expanded to `AABBCC` — the picker never produces one, and
 * padding is what the six-digit slice already does for every other short value.
 */
export function backupCompatibilityPatch(form: BackupCompatibilityPolicy): { backup_compatibility: BackupCompatibilityPolicy } {
  const hex = form.canonical_color_rgba.replace('#', '').toUpperCase().slice(0, 6);
  return {
    backup_compatibility: {
      normalize_color: form.normalize_color,
      canonical_color_rgba: `${hex.padEnd(6, '0')}FF`,
      generic_base_material: form.generic_base_material,
    },
  };
}
