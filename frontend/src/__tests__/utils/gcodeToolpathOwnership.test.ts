import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import {
  parseGcodeToolpath,
  filterLayersByType,
  ToolpathType,
  SEGMENT_TYPED,
  SEGMENT_AFTER_FIRST_LAYER,
  type ToolpathLayer,
} from '../../lib/gcodeToolpath';

const FIXTURES = join(process.cwd(), '..', 'backend', 'app', 'data', 'render_probe');
const load = (name: string) => readFileSync(join(FIXTURES, name), 'utf8');

interface Rec { owner: number; flags: number; type: number; tool: number; x0: number; y0: number; z: number }

/** Every non-travel record with its metadata, in print order. */
function records(layers: ToolpathLayer[]): Rec[] {
  const out: Rec[] = [];
  for (const layer of layers) {
    expect(layer.objectIds).toBeDefined();
    expect(layer.flags).toBeDefined();
    expect(layer.objectIds!.length).toBe(layer.paths.length / 8);
    expect(layer.flags!.length).toBe(layer.paths.length / 8);
    for (let i = 0; i < layer.paths.length; i += 8) {
      if (layer.paths[i + 3] === ToolpathType.travel) continue;
      out.push({
        owner: layer.objectIds![i / 8],
        flags: layer.flags![i / 8],
        type: layer.paths[i + 3],
        tool: layer.paths[i + 7],
        x0: layer.paths[i],
        y0: layer.paths[i + 1],
        z: layer.paths[i + 2],
      });
    }
  }
  return out;
}

const count = (recs: Rec[], pick: (r: Rec) => boolean) => recs.filter(pick).length;

describe('segment ownership', () => {
  const recs = records(parseGcodeToolpath(load('two-objects.gcode')).layers);

  it('assigns every extruding segment to its object', () => {
    expect(count(recs, (r) => r.owner === 101)).toBe(7);
    expect(count(recs, (r) => r.owner === 202)).toBe(73);
    expect(count(recs, (r) => r.owner === -1)).toBe(6);
  });

  it('keeps the filament of each owned segment', () => {
    expect(count(recs, (r) => r.owner === 101 && r.tool === 0)).toBe(6);
    expect(count(recs, (r) => r.owner === 101 && r.tool === 2)).toBe(1);
  });

  it('gives arc chords the owner of their line', () => {
    const chords = recs.filter((r) => r.type === ToolpathType.wall && r.x0 >= 124.9 && r.x0 <= 135.1 && r.z > 0.1);
    expect(chords.length).toBe(72);
    expect(chords.every((r) => r.owner === 202)).toBe(true);
  });

  it('marks start G-code as untyped and before the first layer', () => {
    const purge = recs[0];
    expect(purge.owner).toBe(-1);
    expect(purge.flags & SEGMENT_TYPED).toBe(0);
    expect(purge.flags & SEGMENT_AFTER_FIRST_LAYER).toBe(0);
  });

  it('marks explicitly typed segments after the first layer', () => {
    const skirt = recs.filter((r) => r.type === ToolpathType.skirt);
    expect(skirt).toHaveLength(4);
    expect(skirt.every((r) => (r.flags & SEGMENT_TYPED) !== 0 && (r.flags & SEGMENT_AFTER_FIRST_LAYER) !== 0)).toBe(true);
  });

  it('does not count Custom as a typed feature', () => {
    const single = records(parseGcodeToolpath(load('single-no-markers.gcode')).layers);
    const custom = single[single.length - 1];
    expect(custom.flags & SEGMENT_TYPED).toBe(0);
    expect(count(single, (r) => (r.flags & SEGMENT_TYPED) !== 0)).toBe(8);
  });

  it('parses ownership from CRLF G-code', () => {
    const crlf = load('two-objects.gcode').replaceAll('\r\n', '\n').replaceAll('\n', '\r\n');
    const viaCrlf = records(parseGcodeToolpath(crlf).layers);
    expect(count(viaCrlf, (r) => r.owner === 101)).toBe(7);
    expect(count(viaCrlf, (r) => r.owner === 202)).toBe(73);
  });

  it('stop for another id does not end the open object', () => {
    const gcode = [
      'M83', 'G1 X0 Y0 Z0.2 F600', '; CHANGE_LAYER',
      '; start printing object, unique label id: 7', '; FEATURE: Outer wall',
      'G1 X1 Y0 E0.1', '; stop printing object, unique label id: 8', 'G1 X2 Y0 E0.1',
      '; stop printing object, unique label id: 7', 'G1 X3 Y0 E0.1',
    ].join('\n');
    const owners = records(parseGcodeToolpath(gcode).layers).map((r) => r.owner);
    expect(owners).toEqual([7, 7, -1]);
  });

  it('a layer change inside an open object keeps its owner', () => {
    const gcode = [
      'M83', 'G1 X0 Y0 Z0.2 F600', '; CHANGE_LAYER',
      '; start printing object, unique label id: 7', '; FEATURE: Outer wall',
      'G1 X1 Y0 E0.1', '; CHANGE_LAYER', 'G1 Z0.4', 'G1 X2 Y0 E0.1',
      '; stop printing object, unique label id: 7', 'G1 X3 Y0 E0.1',
    ].join('\n');
    const recs = records(parseGcodeToolpath(gcode).layers);
    expect(recs.map((r) => r.owner)).toEqual([7, 7, -1]);
    // Float32 storage: compare the layer heights at the precision they were written.
    expect(recs.map((r) => Math.round(r.z * 10) / 10)).toEqual([0.2, 0.4, 0.4]);
  });
});

describe('filterLayersByType keeps metadata aligned', () => {
  it('drops supports together with their owners and flags', () => {
    const parsed = parseGcodeToolpath(load('two-objects.gcode'));
    const filtered = filterLayersByType(parsed.layers, new Set([ToolpathType.support]));
    const recs = records(filtered);
    expect(count(recs, (r) => r.owner === 202)).toBe(72);
    expect(count(recs, (r) => r.type === ToolpathType.support)).toBe(0);
  });
});
