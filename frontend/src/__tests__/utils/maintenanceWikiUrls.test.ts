/**
 * Unit tests for getMaintenanceWikiUrl — model-aware wiki URL resolver.
 *
 * Covers the X2D classification (#988): X2D has hardened steel rods like
 * P2S, NOT carbon rods and NOT linear rails. It must resolve to the P2S
 * wiki pages for steel-rod-specific tasks.
 *
 * BamDude divergence from upstream Bambuddy v0.2.3: the signature here is
 * ``(typeCode, printerModel)`` (stable DB identifier) rather than the
 * translated display name, so changing a maintenance type's locale doesn't
 * silently break wiki links. Assertions below therefore pass the snake_case
 * ``type_code`` strings from ``DEFAULT_MAINTENANCE_TYPES`` /
 * ``_ROD_TYPE_REQUIREMENTS`` in ``api/routes/maintenance.py``.
 */

import { describe, it, expect } from 'vitest';
import { getMaintenanceWikiUrl } from '../../utils/maintenanceWikiUrls';

describe('getMaintenanceWikiUrl', () => {
  describe('X2D (#988)', () => {
    it('resolves lubricate_steel_rods to the P2S wiki page', () => {
      expect(getMaintenanceWikiUrl('lubricate_steel_rods', 'X2D')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/lubricate-x-y-z-axis',
      );
    });

    it('resolves clean_steel_rods to the P2S wiki page', () => {
      expect(getMaintenanceWikiUrl('clean_steel_rods', 'X2D')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/lubricate-x-y-z-axis',
      );
    });

    it('resolves check_belt_tension to the P2S wiki page', () => {
      expect(getMaintenanceWikiUrl('check_belt_tension', 'X2D')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/belt-tension',
      );
    });

    it('resolves clean_nozzle to the P2S cold-pull page', () => {
      expect(getMaintenanceWikiUrl('clean_nozzle', 'X2D')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/cold-pull-maintenance-hotend',
      );
    });

    it('resolves check_ptfe_tube via the P2S→X1 PTFE fallback', () => {
      // P2S/X2D have no dedicated PTFE wiki page yet; upstream routes both
      // to the X1 guide. Keep the guard so we notice when Bambu ships one.
      expect(getMaintenanceWikiUrl('check_ptfe_tube', 'X2D')).toBe(
        'https://wiki.bambulab.com/en/x1/maintenance/replace-ptfe-tube',
      );
    });

    it('does not return a carbon-rod wiki URL for X2D', () => {
      // clean_carbon_rods is X1/P1-only; X2D must resolve to null so the
      // task renders without a link rather than pointing at the wrong page.
      expect(getMaintenanceWikiUrl('clean_carbon_rods', 'X2D')).toBeNull();
    });

    it('does not return a linear-rail wiki URL for X2D', () => {
      expect(getMaintenanceWikiUrl('lubricate_linear_rails', 'X2D')).toBeNull();
      expect(getMaintenanceWikiUrl('clean_linear_rails', 'X2D')).toBeNull();
    });
  });

  describe('regression: P2S still maps to P2S wiki pages', () => {
    it('still resolves lubricate_steel_rods for P2S', () => {
      expect(getMaintenanceWikiUrl('lubricate_steel_rods', 'P2S')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/lubricate-x-y-z-axis',
      );
    });

    it('still resolves check_belt_tension for P2S', () => {
      expect(getMaintenanceWikiUrl('check_belt_tension', 'P2S')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/belt-tension',
      );
    });
  });

  describe('regression: other families untouched', () => {
    it('X1C belt tension unchanged', () => {
      expect(getMaintenanceWikiUrl('check_belt_tension', 'X1C')).toBe(
        'https://wiki.bambulab.com/en/x1/maintenance/belt-tension',
      );
    });

    it('H2D belt tension unchanged', () => {
      expect(getMaintenanceWikiUrl('check_belt_tension', 'H2D')).toBe(
        'https://wiki.bambulab.com/en/h2/maintenance/belt-tension',
      );
    });

    it('A1 Mini linear rails unchanged', () => {
      expect(getMaintenanceWikiUrl('lubricate_linear_rails', 'A1 Mini')).toBe(
        'https://wiki.bambulab.com/en/a1-mini/maintenance/lubricate-y-axis',
      );
    });

    it('X1C carbon rods unchanged', () => {
      expect(getMaintenanceWikiUrl('clean_carbon_rods', 'X1C')).toBe(
        'https://wiki.bambulab.com/en/general/carbon-rods-clearance',
      );
    });

    it('P2S still does not resolve linear-rail task', () => {
      // Sanity check: the X2D broadening must not have widened P2S into
      // unrelated task categories.
      expect(getMaintenanceWikiUrl('lubricate_linear_rails', 'P2S')).toBeNull();
    });

    it('clean_build_plate returns the universal PEI page on any model', () => {
      expect(getMaintenanceWikiUrl('clean_build_plate', 'X1C')).toBe(
        'https://wiki.bambulab.com/en/filament-acc/acc/pei-plate-clean-guide',
      );
      expect(getMaintenanceWikiUrl('clean_build_plate', 'X2D')).toBe(
        'https://wiki.bambulab.com/en/filament-acc/acc/pei-plate-clean-guide',
      );
    });
  });

  describe('model name normalisation', () => {
    it('matches X2D regardless of hyphens or spaces', () => {
      expect(getMaintenanceWikiUrl('lubricate_steel_rods', 'x-2d')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/lubricate-x-y-z-axis',
      );
      expect(getMaintenanceWikiUrl('lubricate_steel_rods', 'x 2d')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/lubricate-x-y-z-axis',
      );
    });

    it('lower-cased model still matches', () => {
      expect(getMaintenanceWikiUrl('lubricate_steel_rods', 'x2d')).toBe(
        'https://wiki.bambulab.com/en/p2s/maintenance/lubricate-x-y-z-axis',
      );
    });

    it('returns null for empty/unknown model on model-specific task', () => {
      expect(getMaintenanceWikiUrl('lubricate_steel_rods', null)).toBeNull();
      expect(getMaintenanceWikiUrl('lubricate_steel_rods', '')).toBeNull();
    });

    it('returns null for null typeCode regardless of model', () => {
      expect(getMaintenanceWikiUrl(null, 'X1C')).toBeNull();
    });

    it('returns null for unknown typeCode', () => {
      // Custom user-added maintenance types don't map to a Bambu wiki URL.
      expect(getMaintenanceWikiUrl('custom_user_task', 'X1C')).toBeNull();
    });
  });
});
