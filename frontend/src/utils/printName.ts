/**
 * Append a plate label to the print name. When `plateLabel` is provided (resolved
 * by the caller from the linked archive's plate list — see upstream #881
 * follow-up), it is used verbatim, including the explicit "Plate 1" case on
 * multi-plate 3MFs. Falls back to parsing `plate_N.gcode` from the MQTT
 * gcode_file path, and in that fallback we only show N > 1 because we can't
 * tell from the path alone whether the 3MF is multi-plate.
 */
export function formatPrintName(
  printName: string | null | undefined,
  gcodeFile: string | null | undefined,
  t: (key: string, opts?: Record<string, unknown>) => string,
  plateLabel?: string | null,
): string {
  if (!printName) return '';
  if (plateLabel) return `${printName} — ${plateLabel}`;
  if (!gcodeFile) return printName;
  const match = gcodeFile.match(/plate_(\d+)\.gcode/i);
  if (match && match[1] !== '1') {
    return `${printName} - ${t('printers.plateNumber', { number: match[1] })}`;
  }
  return printName;
}
