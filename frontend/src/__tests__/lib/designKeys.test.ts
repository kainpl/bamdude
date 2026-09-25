import { describe, it, expect } from 'vitest';
import { defaultDesignKeys } from '../../lib/slicerSettings';

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
