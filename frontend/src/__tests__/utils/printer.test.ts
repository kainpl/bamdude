/**
 * Tests for getPrinterImage — model → printer card image resolver.
 *
 * X2D support (#988): both the display name "X2D" and the internal SSDP
 * code "N6" must resolve to /img/printers/x2d.png so the Printers page
 * and PrinterInfoModal show the correct artwork instead of falling back
 * to default.png.
 */

import { describe, it, expect } from 'vitest';
import { getPrinterImage, normalizeModelName } from '../../utils/printer';

describe('getPrinterImage', () => {
  describe('X2D (#988)', () => {
    it('resolves display name "X2D" to x2d.png', () => {
      expect(getPrinterImage('X2D')).toBe('/img/printers/x2d.png');
    });

    it('resolves case-insensitive variants', () => {
      expect(getPrinterImage('x2d')).toBe('/img/printers/x2d.png');
      expect(getPrinterImage(' X2D ')).toBe('/img/printers/x2d.png');
    });

    it('resolves the internal SSDP code "N6" to x2d.png', () => {
      expect(getPrinterImage('N6')).toBe('/img/printers/x2d.png');
    });

    it('does not match X2D on unrelated model strings', () => {
      // Regression guard: a hypothetical future "X2E" model must not
      // silently pick up x2d.png until it's explicitly mapped.
      expect(getPrinterImage('X2E')).toBe('/img/printers/default.png');
    });
  });

  describe('A2L (#1684)', () => {
    it('resolves display name "A2L" to a2l.png', () => {
      expect(getPrinterImage('A2L')).toBe('/img/printers/a2l.png');
      expect(getPrinterImage('a2l')).toBe('/img/printers/a2l.png');
    });

    it('resolves the internal SSDP code "N9" to a2l.png', () => {
      expect(getPrinterImage('N9')).toBe('/img/printers/a2l.png');
    });

    it('does not misclassify A2L as A1', () => {
      expect(getPrinterImage('A2L')).not.toBe('/img/printers/a1.png');
    });
  });

  describe('regression: existing families unchanged', () => {
    it('X1C → x1c.png', () => {
      expect(getPrinterImage('X1C')).toBe('/img/printers/x1c.png');
    });

    it('X1E → x1e.png', () => {
      expect(getPrinterImage('X1E')).toBe('/img/printers/x1e.png');
    });

    it('H2D → h2d.png', () => {
      expect(getPrinterImage('H2D')).toBe('/img/printers/h2d.png');
    });

    it('H2D Pro → h2dpro.png', () => {
      expect(getPrinterImage('H2D Pro')).toBe('/img/printers/h2dpro.png');
    });

    it('P2S → p2s.png (own asset)', () => {
      expect(getPrinterImage('P2S')).toBe('/img/printers/p2s.png');
    });

    it('A1 Mini → a1mini.png (not a1.png)', () => {
      // The "a1mini" branch must run before the generic "a1" branch —
      // the X2D branch was inserted above both and must not break order.
      expect(getPrinterImage('A1 Mini')).toBe('/img/printers/a1mini.png');
    });

    it('null / undefined → default.png', () => {
      expect(getPrinterImage(null)).toBe('/img/printers/default.png');
      expect(getPrinterImage(undefined)).toBe('/img/printers/default.png');
    });

    it('unknown model → default.png', () => {
      expect(getPrinterImage('SomeFuturePrinter')).toBe('/img/printers/default.png');
    });
  });
});

describe('normalizeModelName', () => {
  // The frontend mirror of the backend's `normalize_model_name`. Both sides of
  // any model comparison go through it, or neither: a `Printer.model` column
  // spells "Bambu Lab X1 Carbon" and a 3MF spells "X1C" for the same machine.
  it('resolves the long marketing name to the short one', () => {
    expect(normalizeModelName('Bambu Lab X1 Carbon')).toBe('X1C');
    expect(normalizeModelName('Bambu Lab P1S')).toBe('P1S');
    expect(normalizeModelName('Bambu Lab A1 mini')).toBe('A1 Mini');
  });

  it('resolves an internal code FIRST, or the long-name map would never see it', () => {
    // ⚠️ Order, not coincidence: "C12" is not a long name, so a long-name-first
    // chain returns it unchanged — truthy — and the code map is never reached.
    expect(normalizeModelName('C12')).toBe('P1S');
    expect(normalizeModelName('N6')).toBe('X2D');
  });

  // The frontend map is a superset of the backend's `PRINTER_MODEL_ID_MAP`
  // (backend/app/utils/printer_models.py) — these three A1-series codes are
  // in that map and must be here too.
  it.each([
    ['A11', 'A1'],
    ['A12', 'A1 Mini'],
    ['A04', 'A1 Mini'],
  ])('resolves the A1-series code %s to %s', (code, expected) => {
    expect(normalizeModelName(code)).toBe(expected);
  });

  it('leaves a short name alone and strips the prefix off one it does not know', () => {
    expect(normalizeModelName('X1C')).toBe('X1C');
    expect(normalizeModelName('Bambu Lab Z9')).toBe('Z9');
    expect(normalizeModelName('SomeFuturePrinter')).toBe('SomeFuturePrinter');
  });

  it('has nothing to say about nothing', () => {
    expect(normalizeModelName(null)).toBe('');
    expect(normalizeModelName(undefined)).toBe('');
    expect(normalizeModelName('')).toBe('');
  });
});
