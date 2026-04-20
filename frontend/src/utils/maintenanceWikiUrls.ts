/**
 * Resolve a Bambu Lab wiki URL for a maintenance task based on the printer
 * model. Signature differs from upstream Bambuddy v0.2.3 (#988): BamDude
 * routes by ``type_code`` (the stable identifier from the m002 migration
 * and ``DEFAULT_MAINTENANCE_TYPES`` seed) rather than the translated
 * display name, so changing a maintenance type's locale string doesn't
 * break wiki links.
 *
 * Model families:
 *   - X1, P1         → carbon rods
 *   - P2S, X2D       → hardened steel rods (X2D shares P2S's gantry — #988)
 *   - A1, A1 Mini    → linear rails (Y axis)
 *   - H2D, H2C, H2S  → linear rails (X-axis lubrication)
 *
 * Returns null when no wiki page applies (e.g. carbon-rod task on an H2D),
 * which the caller renders as a task with no clickable help link.
 */
export function getMaintenanceWikiUrl(
  typeCode: string | null,
  printerModel: string | null,
): string | null {
  if (!typeCode) return null;
  const model = (printerModel || '').toUpperCase().replace(/[- ]/g, '');

  const isX1 = model.includes('X1');
  const isP1 = model.includes('P1');
  const isA1Mini = model.includes('A1MINI');
  const isA1 = model.includes('A1') && !isA1Mini;
  const isH2D = model.includes('H2D');
  const isH2C = model.includes('H2C');
  const isH2S = model.includes('H2S');
  const isH2 = isH2D || isH2C || isH2S;
  const isP2S = model.includes('P2S');
  const isX2D = model.includes('X2D');
  // X2D shares the hardened steel rod hardware and belt layout with P2S,
  // so its maintenance routes use the P2S wiki pages until dedicated
  // X2D pages are published by Bambu Lab.
  const isSteelRod = isP2S || isX2D;

  switch (typeCode) {
    case 'lubricate_steel_rods':
    case 'clean_steel_rods':
      if (isSteelRod) return 'https://wiki.bambulab.com/en/p2s/maintenance/lubricate-x-y-z-axis';
      return null;

    case 'lubricate_linear_rails':
    case 'clean_linear_rails':
      if (isA1Mini) return 'https://wiki.bambulab.com/en/a1-mini/maintenance/lubricate-y-axis';
      if (isA1) return 'https://wiki.bambulab.com/en/a1/maintenance/lubricate-y-axis';
      if (isH2) return 'https://wiki.bambulab.com/en/h2/maintenance/x-axis-lubrication';
      return null;

    case 'clean_nozzle':
      if (isX1 || isP1) return 'https://wiki.bambulab.com/en/x1/troubleshooting/nozzle-clog';
      if (isA1Mini || isA1) return 'https://wiki.bambulab.com/en/a1-mini/troubleshooting/nozzle-clog';
      if (isH2) return 'https://wiki.bambulab.com/en/h2/maintenance/nozzl-cold-pull-maintenance-and-cleaning';
      if (isSteelRod) return 'https://wiki.bambulab.com/en/p2s/maintenance/cold-pull-maintenance-hotend';
      return 'https://wiki.bambulab.com/en/x1/troubleshooting/nozzle-clog';

    case 'check_belt_tension':
      if (isX1) return 'https://wiki.bambulab.com/en/x1/maintenance/belt-tension';
      if (isP1) return 'https://wiki.bambulab.com/en/p1/maintenance/p1p-maintenance';
      if (isA1Mini) return 'https://wiki.bambulab.com/en/a1-mini/maintenance/belt_tension';
      if (isA1) return 'https://wiki.bambulab.com/en/a1/maintenance/belt_tension';
      if (isH2D) return 'https://wiki.bambulab.com/en/h2/maintenance/belt-tension';
      if (isH2C) return 'https://wiki.bambulab.com/en/h2c/maintenance/belt-tension';
      if (isH2S) return 'https://wiki.bambulab.com/en/h2s/maintenance/belt-tension';
      if (isSteelRod) return 'https://wiki.bambulab.com/en/p2s/maintenance/belt-tension';
      return 'https://wiki.bambulab.com/en/x1/maintenance/belt-tension';

    case 'clean_carbon_rods':
      if (isX1 || isP1) return 'https://wiki.bambulab.com/en/general/carbon-rods-clearance';
      return null;

    case 'clean_build_plate':
      return 'https://wiki.bambulab.com/en/filament-acc/acc/pei-plate-clean-guide';

    case 'check_ptfe_tube':
      if (isX1 || isP1) return 'https://wiki.bambulab.com/en/x1/maintenance/replace-ptfe-tube';
      if (isA1Mini || isA1) return 'https://wiki.bambulab.com/en/a1-mini/maintenance/ptfe-tube';
      if (isH2D) return 'https://wiki.bambulab.com/en/h2/maintenance/replace-ptfe-tube-on-h2d-printer';
      if (isH2S) return 'https://wiki.bambulab.com/en/h2s/maintenance/replace-ptfe-tube-on-h2s-printer';
      if (isH2C) return 'https://wiki.bambulab.com/en/h2/maintenance/replace-ptfe-tube-on-h2d-printer'; // H2C uses H2D guide
      if (isSteelRod) return 'https://wiki.bambulab.com/en/x1/maintenance/replace-ptfe-tube'; // P2S/X2D use the X1 PTFE guide
      return 'https://wiki.bambulab.com/en/x1/maintenance/replace-ptfe-tube';

    default:
      return null;
  }
}
