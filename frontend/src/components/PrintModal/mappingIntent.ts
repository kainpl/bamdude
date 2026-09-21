import type { PerPrinterConfig } from '../../hooks/useMultiPrinterFilamentMapping';

/** A rejected display match must not erase the physical choice before validation. */
export function mappingWithManualChoices(mapping: number[] | undefined, manual: Record<number, number>): number[] | undefined {
  const slots = Object.keys(manual).map(Number).filter(slot => slot > 0);
  if (!slots.length) return mapping;
  const result = Array.from({ length: Math.max(mapping?.length ?? 0, ...slots) }, (_, i) => mapping?.[i] ?? -1);
  for (const slot of slots) result[slot - 1] = manual[slot];
  return result;
}

/** Physical tray IDs have meaning only inside the target where they were picked. */
export function targetHasManualMapping(input: {
  printerCount: number; multiPlate: boolean; plateId: number | null;
  manual: Record<number, number>; byPlate: Record<number, Record<number, number>>;
  config?: PerPrinterConfig; storedPinned: boolean;
}): boolean {
  if (input.printerCount > 1) {
    return !input.multiPlate && !!input.config && !input.config.useDefault && !input.config.autoConfigured;
  }
  if (input.multiPlate) return input.plateId !== null && Object.keys(input.byPlate[input.plateId] ?? {}).length > 0;
  return Object.keys(input.manual).length > 0 || input.storedPinned;
}
