import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { parseGcodeToolpath, ToolpathType } from '../../lib/gcodeToolpath';
import { selectSegments, paletteFromGcode, fitsBox, HIDDEN_TYPES } from '../../render-worker/select';
import type { Bounds } from '../../render-worker/protocol';

const FIXTURES = join(process.cwd(), '..', 'backend', 'app', 'data', 'render_probe');
const load = (name: string) => readFileSync(join(FIXTURES, name), 'utf8');

/** Bounds come from a Float32Array: 0.2 reads back as 0.20000000298. */
const rounded = (b: Bounds | null) => b && { min: b.min.map((v) => +v.toFixed(3)), max: b.max.map((v) => +v.toFixed(3)) };

describe('selectSegments', () => {
  const parsed = parseGcodeToolpath(load('two-objects.gcode'));

  it('selects one object with its filaments', () => {
    const sel = selectSegments(parsed, { kind: 'object', id: 101 });
    expect(sel.segments).toBe(7);
    expect(sel.tools).toEqual({ 0: 6, 2: 1 });
    expect(rounded(sel.bounds)).toEqual({ min: [100, 100, 0.2], max: [110, 110, 0.4] });
  });

  it('re-keys the record type to tool + 1 so filaments never merge vertices', () => {
    const sel = selectSegments(parsed, { kind: 'object', id: 101 });
    const types = new Set<number>();
    for (const layer of sel.layers) for (let i = 0; i < layer.paths.length; i += 8) types.add(layer.paths[i + 3]);
    expect([...types].sort()).toEqual([1, 3]);
  });

  it('hides supports, skirt, raft, prime tower and travels', () => {
    expect([...HIDDEN_TYPES].sort((a, b) => a - b)).toEqual(
      [ToolpathType.travel, ToolpathType.skirt, ToolpathType.support, ToolpathType.raft, ToolpathType.primeTower].sort((a, b) => a - b),
    );
    const sel = selectSegments(parsed, { kind: 'object', id: 202 });
    expect(sel.segments).toBe(72);
  });

  it('selects nothing for an id the G-code does not mark', () => {
    const sel = selectSegments(parsed, { kind: 'object', id: 999 });
    expect(sel.segments).toBe(0);
    expect(sel.bounds).toBeNull();
  });

  it('model selection keeps only typed segments after the first layer', () => {
    const single = parseGcodeToolpath(load('single-no-markers.gcode'));
    const sel = selectSegments(single, { kind: 'model' });
    expect(sel.segments).toBe(8);
    expect(rounded(sel.bounds)).toEqual({ min: [120, 120, 0.2], max: [125, 125, 0.4] });
  });

  it('model selection on a marked file keeps no service toolpaths', () => {
    // Everything outside objects there is purge, skirt or prime tower.
    expect(selectSegments(parsed, { kind: 'model' }).segments).toBe(0);
  });
});

describe('paletteFromGcode', () => {
  it('reads filament_colour by T index, keeping unused slots', () => {
    expect(paletteFromGcode(load('two-objects.gcode'))).toEqual(['#C0C0C0', '#161616', '#00AE42']);
  });

  it('drops the alpha byte of #RRGGBBAA', () => {
    expect(paletteFromGcode('; filament_colour = #00AE42FF;#C0C0C080\n')).toEqual(['#00AE42', '#C0C0C0']);
  });

  it('returns an empty palette without the header line', () => {
    expect(paletteFromGcode('G1 X0\n')).toEqual([]);
  });
});

describe('fitsBox', () => {
  const bounds = { min: [120, 120, 0.2] as [number, number, number], max: [125, 125, 0.4] as [number, number, number] };

  it('accepts bounds inside the object box with tolerance', () => {
    expect(fitsBox(bounds, [119, 119, 126, 126])).toBe(true);
    expect(fitsBox(bounds, [121, 121, 124, 124], 2)).toBe(true);
  });

  it('rejects bounds that reach outside the box', () => {
    expect(fitsBox(bounds, [0, 0, 10, 10])).toBe(false);
    expect(fitsBox({ min: [18, 1, 0.2], max: [218, 125, 0.4] }, [119, 119, 126, 126])).toBe(false);
  });
});
