/**
 * Every place a spool's colour NAME is shown or filtered on tells the resolver
 * when the name is only Spoolman's subtype (upstream e4a9ef45, #3090) — without
 * the flag "Silk+" read as a colour on the Filaments page and in Assign Spool.
 */
import { describe, expect, it } from 'vitest';

import inventory from '../../pages/InventoryPage.tsx?raw';
import assign from '../../components/AssignSpoolModal.tsx?raw';

describe('colour-name call sites pass color_name_is_synthesized', () => {
  it('on the Filaments page', () => {
    // The facet pairs are the built-in inventory's (server mode), which never
    // synthesises a name, so they carry no flag.
    const calls = (inventory.match(/resolveSpoolColorName\([^)]*\)/g) ?? []).filter((c) => !c.includes('pair.'));
    expect(calls.length).toBeGreaterThan(0);
    for (const call of calls) expect(call).toContain('color_name_is_synthesized');
  });

  it('in the Filaments search, with the catalogue version as a dependency', () => {
    const search = inventory.slice(inventory.indexOf('// Global search'));
    const block = search.slice(0, search.indexOf('return filtered;'));
    expect(block).toContain('resolveSpoolColorName(s.color_name, s.rgba, s.color_name_is_synthesized)');
    const deps = search.slice(search.indexOf('return filtered;'), search.indexOf('return filtered;') + 600);
    expect(deps).toContain('colorCatalogVersion]');
  });

  it('in Assign Spool', () => {
    expect(assign).not.toContain("{spool.color_name || ''}");
    expect(assign).toContain('resolveSpoolColorName(spool.color_name, spool.rgba, spool.color_name_is_synthesized)');
  });
});
