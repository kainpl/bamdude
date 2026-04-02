import type { TFunction } from 'i18next';

/**
 * Maps DB maintenance type names to i18n keys.
 * DB stores English names; frontend displays localized versions.
 */
const NAME_TO_KEY: Record<string, string> = {
  'Lubricate Carbon Rods': 'lubricateCarbonRods',
  'Lubricate Linear Rails': 'lubricateRails',
  'Clean Nozzle/Hotend': 'cleanNozzle',
  'Check Belt Tension': 'checkBelts',
  'Clean Build Plate': 'cleanBuildPlate',
  'Check Extruder Gears': 'checkExtruder',
  'Check Cooling Fans': 'checkCooling',
  'General Inspection': 'generalInspection',
  'Clean Carbon Rods': 'cleanCarbonRods',
  'Lubricate Steel Rods': 'lubricateSteelRods',
  'Clean Steel Rods': 'cleanSteelRods',
  'Clean Linear Rails': 'cleanLinearRails',
  'Check PTFE Tube': 'checkPtfeTube',
  'Replace HEPA Filter': 'replaceHepaFilter',
  'Replace Carbon Filter': 'replaceCarbonFilter',
  'Lubricate Left Nozzle Rail': 'lubricateLeftNozzleRail',
};

/** Get localized maintenance type name. Falls back to DB value if no mapping. */
export function getMaintenanceTypeName(dbName: string, t: TFunction): string {
  const key = NAME_TO_KEY[dbName];
  return key ? t(`maintenance.types.${key}`, dbName) : dbName;
}

/** Get localized maintenance type description. Falls back to DB value if no mapping. */
export function getMaintenanceTypeDescription(dbName: string, dbDescription: string | null, t: TFunction): string {
  const key = NAME_TO_KEY[dbName];
  return key ? t(`maintenanceDescriptions.${key}`, dbDescription || '') : dbDescription || '';
}
