/**
 * The slice dialog's side of matching presets on what they declare (upstream
 * e9daa212, #2982). The decisions live in `utils/slicePresetPicker` and are
 * tested there; this pins the wiring.
 */
import { describe, expect, it } from 'vitest';

import modalSource from '../../components/SliceModal.tsx?raw';
import { applyChoice, POLICIES, type ChoiceField } from '../../components/slicing-setup/choiceState';
import { buildPresetField } from '../../components/slicing-setup/presetSets';
import { EMPTY_COMPATIBILITY_INDEX } from '../../utils/slicerPrinterMatch';
import type { UnifiedPresetsResponse } from '../../api/client';

describe('SliceModal — keeping a filament pick', () => {
  it('re-picks an auto-picked slot whose preset states another material', () => {
    // Compatible with the printer is not on its own a reason to keep it: that
    // is how a PETG profile survived on a PLA plate through every re-pick.
    expect(modalSource).toContain('statesDifferentMaterial(p, slot.type)');
  });

  it("never overrules the user's own pick", () => {
    // Printing PETG on a plate labelled PLA is a thing people do on purpose —
    // the rule corrects the auto-pick only. A pipeline is as deliberate.
    // Behaviour: `SliceModalExplicitChoice.test.tsx`.
    // The dialog hands a pick by hand and an applied set to the choice state…
    expect(modalSource).toContain("kind: 'personPicked'");
    expect(modalSource).toContain("kind: 'pipelineApplied'");
    // …which keeps either through a re-check that would replace an automatic pick.
    const petg = { source: 'standard' as const, id: 'Generic PETG' };
    const repick = () => ({ source: 'standard' as const, id: 'Generic PLA' });
    for (const origin of ['person', 'pipeline'] as const) {
      const field: ChoiceField = { ref: petg, origin, gone: false };
      const refresh = { kind: 'refresh' as const, listed: () => true, usable: () => false, repick };
      expect(applyChoice(field, refresh, POLICIES.sliceFilament).field).toEqual(field);
      const printer = { kind: 'printerChanged' as const, usable: () => false, repick };
      expect(applyChoice(field, printer, POLICIES.sliceFilament).field).toEqual(field);
    }
  });

  it('forgets which slots were chosen when the plate layout changes', () => {
    // A new slot count picks every slot again with an automatic origin.
    expect(modalSource).toContain("forEachFilament((slot) => ({ kind: 'reset', pick: pickFilament(data, slot) }))");
    const person: ChoiceField = { ref: { source: 'standard', id: 'x' }, origin: 'person', gone: false };
    expect(applyChoice(person, { kind: 'reset', pick: null }, POLICIES.sliceFilament).field.origin).toBe('auto');
  });
});

describe('SliceModal — a printer filter that would empty the dropdown', () => {
  it('shows the list unfiltered instead', () => {
    // A visible preset for the wrong printer can be changed; an empty
    // dropdown cannot.
    // The process and filament fields are built by the family with a printer fit…
    expect(modalSource).toContain("buildPresetField({ listing: data, slot: 'process', fit,");
    // …which shows the whole bound, with its note, when nothing fits (R2, #2982).
    const empty = { printer: [], process: [], filament: [] };
    const listing = {
      local: empty,
      orca_cloud: empty,
      cloud: empty,
      standard: { ...empty, process: [{ id: 'a1', name: 'a1', source: 'standard', compatible_printers: ['Bambu Lab A1 0.4 nozzle'] }] },
    } as unknown as UnifiedPresetsResponse;
    const field = buildPresetField({
      listing,
      slot: 'process',
      fit: { printerName: 'Bambu Lab X1 Carbon 0.4 nozzle', compatIndex: EMPTY_COMPATIBILITY_INDEX, mode: 'group' },
      filters: [],
      pinned: null,
    });
    expect(field.noFit).toBe(true);
    expect(field.sections.flatMap((s) => s.entries).map((p) => p.id)).toEqual(['a1']);
  });
});
