/**
 * The slice dialog tells the panel and the backend what the file's own
 * settings are doing (upstream b1f5ec96, #2942).
 *
 * ⚠️ BamDude keeps its own default — the designer's intent starts ticked — so
 * this pins the WIRING, not upstream's "nothing until asked": the panel sees
 * which of the file's values are on (its greying rules read them), the ticks
 * in the panel and in the dialog's own list are one state, and the request
 * goes out through `designOverridesField`, which is what sends an empty list
 * when everything on offer was unticked.
 */
import { describe, it, expect } from 'vitest';

import modalSource from '../../components/SliceModal.tsx?raw';

describe('SliceModal — the file\'s own settings', () => {
  it('hands the panel the offered settings and which of them are on', () => {
    const panel = modalSource.slice(modalSource.indexOf('<SlicerSettingsPanel'));
    const props = panel.slice(0, panel.indexOf('/>'));
    expect(props).toContain('sourceOverrides={designOverrides}');
    expect(props).toContain('sourceSelected={designKeys}');
    expect(props).toContain('onToggleSource=');
  });

  it('builds the request field through the one helper', () => {
    expect(modalSource).toContain('...designOverridesField(useEmbedded, designOverrides, designKeys)');
    // The old spread dropped an emptied list, which the backend then read as
    // "an old client" and carried the file's supports regardless.
    expect(modalSource).not.toContain('designKeys.size > 0 ? { design_overrides');
  });

  it('keeps BamDude\'s default: the designer\'s intent starts ticked', () => {
    expect(modalSource).toContain('setDesignKeys(defaultDesignKeys(designOverrides))');
  });
});
