/**
 * The slice dialog's side of matching presets on what they declare (upstream
 * e9daa212, #2982). The decisions live in `utils/slicePresetPicker` and are
 * tested there; this pins the wiring.
 */
import { describe, expect, it } from 'vitest';

import modalSource from '../../components/SliceModal.tsx?raw';

describe('SliceModal — keeping a filament pick', () => {
  it('re-picks an auto-picked slot whose preset states another material', () => {
    // Compatible with the printer is not on its own a reason to keep it: that
    // is how a PETG profile survived on a PLA plate through every re-pick.
    expect(modalSource).toContain('statesDifferentMaterial(p, slot.type)');
  });

  it("never overrules the user's own pick", () => {
    // Printing PETG on a plate labelled PLA is a thing people do on purpose —
    // the rule corrects the auto-pick only. A pipeline is as deliberate.
    expect(modalSource).toContain('!explicitFilamentSlots.current.has(i)');
    expect(modalSource).toContain('explicitFilamentSlots.current.add(idx)');
    expect(modalSource).toContain('explicitFilamentSlots.current.add(i)');
  });

  it('forgets which slots were chosen when the plate layout changes', () => {
    expect(modalSource).toContain('explicitFilamentSlots.current = new Set()');
  });
});

describe('SliceModal — a printer filter that would empty the dropdown', () => {
  it('shows the list unfiltered instead', () => {
    // A visible preset for the wrong printer can be changed; an empty
    // dropdown cannot.
    expect(modalSource).toContain('if (compatSections.length === 0 && other.length > 0)');
    expect(modalSource).toContain('return { sections: unfiltered, otherEntries: [] }');
  });
});
