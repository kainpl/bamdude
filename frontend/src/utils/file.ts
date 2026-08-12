/**
 * Formats a byte count into a human-readable string (e.g. `1.5 MB`).
 *
 * @param bytes - The number of bytes to format.
 * @returns A formatted string with the appropriate unit (B, KB, MB, GB, or TB).
 */
export function formatFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 B';

  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  const k = 1024;
  const i = Math.floor(Math.log(bytes) / Math.log(k));

  const size = bytes / Math.pow(k, i);

  // No decimals for bytes, 1 decimal for larger units
  return i === 0
    ? `${size} ${units[i]}`
    : `${size.toFixed(1)} ${units[i]}`;
}

/**
 * The date to show and sort a library file by (#2680).
 *
 * For an uploaded file `created_at` is the honest answer — it is the moment the
 * file arrived. For an external (mapped / NAS) file it is not: a folder scan
 * writes the same instant onto every row it discovers, so a whole imported tree
 * ties and sorts arbitrarily instead of newest-first. `fs_modified_at` carries
 * the real on-disk mtime for exactly those rows and is null everywhere else.
 *
 * Kept here rather than inlined so the two renderers and the comparator cannot
 * drift apart — a list that sorts on one field and displays another looks
 * broken in a way that is very hard to read off the screen.
 */
export function fileActivityAt(file: {
  created_at: string;
  fs_modified_at?: string | null;
}): string {
  return file.fs_modified_at || file.created_at;
}
