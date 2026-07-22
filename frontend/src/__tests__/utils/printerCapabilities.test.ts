/**
 * Unit tests for the per-model auto-calibration capability matrix — the
 * frontend mirror of backend `printer_models.py`. Gates the print dialog's
 * 3-position off/auto/on control.
 *
 * Bed + flow auto: {A2L, P2S, H2S, H2C, H2D, H2D Pro, X2D} (independent of
 * nozzle count). Nozzle-offset auto: dual-nozzle only {H2C, H2D, H2D Pro, X2D}.
 */

import { describe, it, expect } from 'vitest';
import {
  autoCalibrationCaps,
  supportsAutoBedLeveling,
  supportsAutoFlowCali,
  supportsAutoNozzleOffset,
} from '../../utils/printerCapabilities';

describe('printerCapabilities auto-calibration matrix', () => {
  describe('bed + flow auto (same matrix)', () => {
    it.each(['A2L', 'P2S', 'H2S', 'H2C', 'H2D', 'H2D Pro', 'X2D'])('is supported for %s', (m) => {
      expect(supportsAutoBedLeveling(m)).toBe(true);
      expect(supportsAutoFlowCali(m)).toBe(true);
    });

    it.each(['N9', 'N7', 'O1S', 'O1C', 'O1C2', 'O1D', 'O1E', 'O2D', 'N6'])(
      'is supported for internal code %s',
      (m) => {
        expect(supportsAutoBedLeveling(m)).toBe(true);
        expect(supportsAutoFlowCali(m)).toBe(true);
      },
    );

    it.each(['X1C', 'X1', 'P1S', 'P1P', 'A1', 'A1 Mini', '', null, undefined])(
      'is NOT supported for %s',
      (m) => {
        expect(supportsAutoBedLeveling(m)).toBe(false);
        expect(supportsAutoFlowCali(m)).toBe(false);
      },
    );
  });

  describe('nozzle-offset auto (dual-nozzle only)', () => {
    it.each(['H2C', 'H2D', 'H2D Pro', 'X2D', 'O1C', 'O1D', 'O1E', 'O2D', 'N6'])(
      'is supported for %s',
      (m) => {
        expect(supportsAutoNozzleOffset(m)).toBe(true);
      },
    );

    it.each(['A2L', 'P2S', 'H2S', 'N9', 'N7', 'O1S'])(
      'is NOT supported for single-nozzle auto-capable %s (offset needs two nozzles)',
      (m) => {
        expect(supportsAutoBedLeveling(m)).toBe(true);
        expect(supportsAutoNozzleOffset(m)).toBe(false);
      },
    );

    it.each(['X1C', 'P1S', 'A1', '', null, undefined])('is NOT supported for %s', (m) => {
      expect(supportsAutoNozzleOffset(m)).toBe(false);
    });
  });

  describe('normalization', () => {
    it('is case- and dash/space-insensitive', () => {
      expect(supportsAutoBedLeveling('h2d pro')).toBe(true);
      expect(supportsAutoBedLeveling(' H2D-Pro ')).toBe(true);
      expect(supportsAutoNozzleOffset('x2d')).toBe(true);
    });
  });

  describe('autoCalibrationCaps', () => {
    it('reports per-field caps for a dual-nozzle auto model (X2D)', () => {
      expect(autoCalibrationCaps('X2D')).toEqual({
        bed_levelling: true,
        flow_cali: true,
        nozzle_offset_cali: true,
      });
    });

    it('reports bed/flow but not nozzle for a single-nozzle auto model (P2S)', () => {
      expect(autoCalibrationCaps('P2S')).toEqual({
        bed_levelling: true,
        flow_cali: true,
        nozzle_offset_cali: false,
      });
    });

    it('reports all false for a non-auto model (P1S)', () => {
      expect(autoCalibrationCaps('P1S')).toEqual({
        bed_levelling: false,
        flow_cali: false,
        nozzle_offset_cali: false,
      });
    });
  });
});
