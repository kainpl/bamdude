import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { runJob } from '../../part-render/job';
import { RenderError, type RenderJob } from '../../part-render/protocol';

const FIXTURES = join(process.cwd(), '..', 'backend', 'app', 'data', 'render_probe');
const gcode = (name: string) => readFileSync(join(FIXTURES, name), 'utf8');
// outputBytes covers the PNG frames AND the manifest reserve (MANIFEST_MAX)
const job = (objects: RenderJob['objects'], outputBytes = 8 * 2 ** 20): RenderJob => ({ size: 64, objects, outputBytes });
const reasonOf = (run: () => unknown) => {
  try { run(); } catch (e) { return (e as RenderError).reason; }
  return 'no error';
};

describe('runJob', () => {
  it('renders each marked object and reports an unmarked id as missing', () => {
    const pngs = new Map<number, Uint8Array>();
    const m = runJob(job([{ id: 101, mode: 'toolpath' }, { id: 202, mode: 'toolpath' }, { id: 999, mode: 'toolpath' }]),
      gcode('two-objects.gcode'), (id, png) => pngs.set(id, png));
    expect([...pngs.keys()]).toEqual([101, 202]);
    expect(m.renderer).toBe(2);
    expect(m.objects.map((o) => [o.id, o.method])).toEqual([[101, 'toolpath'], [202, 'toolpath'], [999, 'missing']]);
    expect(m.objects[2]).toMatchObject({ reason: 'empty_selection' });
    expect(m.objects[0]).toMatchObject({ tools: [0, 2], width: 64, height: 64, bytes: pngs.get(101)!.length });
  });

  it('renders the model of a markerless single object only inside its plate box', () => {
    const inside = runJob(job([{ id: 1, mode: 'model', bbox: [119, 119, 126, 126] }]), gcode('single-no-markers.gcode'), () => {});
    expect(inside.objects[0].method).toBe('model');
    const outside = runJob(job([{ id: 1, mode: 'model', bbox: [0, 0, 5, 5] }]), gcode('single-no-markers.gcode'), () => {});
    expect(outside.objects[0]).toMatchObject({ method: 'missing', reason: 'model_unproven' });
  });

  it('reports a fully transparent render as missing empty_render, without a PNG', () => {
    const m = runJob(job([{ id: 101, mode: 'toolpath' }]), gcode('two-objects.gcode'),
      () => { throw new Error('no PNG for an empty render'); }, () => {});
    expect(m.objects[0]).toMatchObject({ method: 'missing', reason: 'empty_render' });
  });

  it('refuses a palette without the colour of a used tool as parse_failed', () => {
    const noPalette = gcode('two-objects.gcode').replace(/^; filament_colour = .*$/m, '; filament_colour = ');
    expect(noPalette).not.toBe(gcode('two-objects.gcode'));
    expect(reasonOf(() => runJob(job([{ id: 101, mode: 'toolpath' }]), noPalette, () => {}))).toBe('parse_failed');
  });

  it('stops at the output budget with invalid_output', () => {
    expect(reasonOf(() => runJob(job([{ id: 101, mode: 'toolpath' }], 10), gcode('two-objects.gcode'), () => {}))).toBe('invalid_output');
  });

  it('gives the same bytes twice', () => {
    const a: Uint8Array[] = [], b: Uint8Array[] = [];
    runJob(job([{ id: 101, mode: 'toolpath' }]), gcode('two-objects.gcode'), (_, p) => a.push(p));
    runJob(job([{ id: 101, mode: 'toolpath' }]), gcode('two-objects.gcode'), (_, p) => b.push(p));
    expect(Buffer.from(a[0]).equals(Buffer.from(b[0]))).toBe(true);
  });
});
