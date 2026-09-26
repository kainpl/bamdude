import { describe, it, expect } from 'vitest';
import { defaultDesignKeys, designOverridesField } from '../../lib/slicerSettings';

describe('defaultDesignKeys — which of the file\'s own settings start ticked', () => {
  it('ticks design intent, never a machine-coupled or preset-defining key (upstream 7e77bf58)', () => {
    const keys = defaultDesignKeys([
      { key: 'wall_loops', value: '5', printer_coupled: false },
      { key: 'outer_wall_speed', value: '200', printer_coupled: true },
      // The picked "0.08mm High Quality" IS its layer height: the file's 0.2
      // must not quietly slice over it.
      { key: 'layer_height', value: '0.2', printer_coupled: false, preset_defining: true },
      { key: 'initial_layer_print_height', value: '0.3', printer_coupled: false, preset_defining: true },
    ]);
    expect([...keys]).toEqual(['wall_loops']);
  });

  it('treats an older response without the flag as not preset-defining', () => {
    expect([...defaultDesignKeys([{ key: 'wall_loops', value: '5', printer_coupled: false }])]).toEqual(['wall_loops']);
  });
});

describe('designOverridesField — what the slice request says about the file\'s settings (upstream b1f5ec96, #2942)', () => {
  const offered = [
    { key: 'wall_loops', value: '5', printer_coupled: false },
    { key: 'enable_support', value: '1', printer_coupled: false },
  ];

  it('sends the ticked keys', () => {
    expect(designOverridesField(false, offered, new Set(['wall_loops']))).toEqual({ design_overrides: ['wall_loops'] });
  });

  it('sends an EMPTY list when the file offered some and none is ticked', () => {
    // Not the same answer as leaving the field out: the backend reads an empty
    // list as "shown and declined", which also stands the support carry down.
    expect(designOverridesField(false, offered, new Set())).toEqual({ design_overrides: [] });
  });

  it('leaves the field out when the file offered nothing', () => {
    // An OrcaSlicer export records no deviations; with nothing to decline the
    // support carry must stay whole.
    expect(designOverridesField(false, [], new Set())).toEqual({});
  });

  it('leaves the field out on the embedded-settings path', () => {
    // That path sends no process JSON for the overrides to patch.
    expect(designOverridesField(true, offered, new Set(['wall_loops']))).toEqual({});
  });
});
