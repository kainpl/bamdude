import type { BackupCompatibilityPolicy } from '../api/client';

/** Normalises what the Edit Printer form holds for the backup-compatibility policy into what PATCH /printers/{id} accepts. */
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
