import { describe, it, expect } from 'vitest';
import { buildSegmentData } from '../../lib/vendor/toolpathRenderer.js';
import {
  frameCamera, geometryBounds, isEmpty, lighting, segmentVertices, SegmentVertices, RenderTarget, drawSegments, rasterTriangle, resolve,
} from '../../part-render/raster';
import type { Bounds } from '../../part-render/protocol';

/**
 * One layer at z with records [x0, y0, z0, type, x1, y1, z1, tool]. The colour key
 * is the record TYPE (vType): in production selectSegments re-keys it to tool + 1,
 * so tests put the colour key there directly. Type 0 is a travel and is never drawn.
 */
function layer(z: number, ...records: number[][]) {
  return { z, paths: new Float32Array(records.flat()), widths: records.map(() => 0.42) };
}
function bounds(layers: ReturnType<typeof layer>[]): Bounds {
  const min: [number, number, number] = [Infinity, Infinity, Infinity];
  const max: [number, number, number] = [-Infinity, -Infinity, -Infinity];
  for (const l of layers) for (let i = 0; i < l.paths.length; i += 8) for (const at of [i, i + 4]) for (let a = 0; a < 3; a++) {
    min[a] = Math.min(min[a], l.paths[at + a]); max[a] = Math.max(max[a], l.paths[at + a]);
  }
  return { min, max };
}
function render(layers: ReturnType<typeof layer>[], colours: Record<number, [number, number, number]>, size = 64) {
  const data = buildSegmentData(layers, 0.42);
  const rgb = new Float32Array(data.nV * 3);
  for (let v = 0; v < data.nV; v++) rgb.set(colours[data.meta.vType[v]] ?? [1, 0, 1], v * 3);
  const target = new RenderTarget(size, 2);
  target.clear();
  drawSegments(data, rgb, frameCamera(geometryBounds(data)!), target);
  return resolve(target);
}
const alphaAt = (img: Uint8Array, size: number, x: number, y: number) => img[(y * size + x) * 4 + 3];

describe('lighting', () => {
  it('is ambient + emission for a normal facing away from both lights', () => {
    // eye position behind the viewer: the specular term is exactly zero
    expect(lighting(0, 0, 10, 0.4574957, -0.4574957, -0.7624929)).toBeCloseTo(0.45, 9);
  });
  it('adds the full top diffuse for a normal along the top light', () => {
    const n = [-0.4574957, 0.4574957, 0.7624929];
    const front = Math.max(n[0] * 0.6985074 + n[1] * 0.1397015 + n[2] * 0.6985074, 0) * 0.18;
    expect(lighting(0, 0, -10, n[0], n[1], n[2])).toBeGreaterThan(0.45 + 0.48 + front - 1e-6);
  });
});

describe('segmentVertices', () => {
  it('puts the pointed caps half a width beyond each end of a straight segment', () => {
    const layers = [layer(0.2, [0, 0, 0.2, 1, 10, 0, 0.2, 0])];
    const data = buildSegmentData(layers, 0.42);
    const cam = frameCamera(bounds(layers));
    const out = new SegmentVertices();
    segmentVertices(data, data.segIndex[0], new Float32Array(data.nV * 3).fill(1), cam, 100, out);
    expect(out.ok).toBe(true);
    // vertex 2 is the start cap, vertex 7 the end cap; 0 and 4 sit beside the two endpoints
    const span = Math.hypot(out.x[7] - out.x[2], out.y[7] - out.y[2]);
    const body = Math.hypot(out.x[4] - out.x[0], out.y[4] - out.y[0]);
    expect(span).toBeGreaterThan(body);
  });

  it('zero-length and vertical segments draw without NaN', () => {
    // A NaN lands in the Float32 depth / colour buffers; in the Uint8 image it reads as 0 (black), so the
    // buffers are what is checked, and each degenerate kind is drawn on its own (review focus 4).
    const cases: Record<string, number[][]> = {
      'zero length': [[5, 5, 0.2, 1, 5, 5, 0.2, 0]],
      vertical: [[5, 5, 0.2, 1, 5, 5, 3, 0]],
      mixed: [[5, 5, 0.2, 1, 5, 5, 0.2, 0], [5, 5, 0.2, 1, 5, 5, 3, 0], [0, 0, 0.2, 1, 10, 10, 0.2, 0]],
    };
    for (const [name, records] of Object.entries(cases)) {
      const data = buildSegmentData([layer(0.2, ...records)], 0.42);
      const target = new RenderTarget(64, 2);
      target.clear();
      drawSegments(data, new Float32Array(data.nV * 3).fill(0.5), frameCamera(geometryBounds(data)!), target);
      expect(target.depth.some(Number.isNaN), `${name}: depth`).toBe(false);
      expect(target.rgb.some(Number.isNaN), `${name}: colour`).toBe(false);
      const img = resolve(target);
      let covered = 0, black = 0;
      for (let i = 0; i < img.length; i += 4) {
        if (img[i + 3] === 0) continue;
        covered++;
        if (img[i] + img[i + 1] + img[i + 2] === 0) black++;
      }
      expect(covered, `${name}: drawn`).toBeGreaterThan(0);
      expect(black, `${name}: black pixels`).toBe(0);
    }
  });
});

describe('drawSegments + resolve', () => {
  it('leaves the background transparent', () => {
    const img = render([layer(0.2, [0, 0, 0.2, 1, 10, 0, 0.2, 0])], { 1: [1, 0, 0] });
    expect(alphaAt(img, 64, 0, 0)).toBe(0);
    expect(alphaAt(img, 64, 63, 63)).toBe(0);
  });

  it('keeps the nearer of two overlapping triangles whatever the drawing order', () => {
    for (const order of [['far', 'near'], ['near', 'far']]) {
      const target = new RenderTarget(4, 1);
      target.clear();
      const s = new SegmentVertices();
      const tri = (z: number, r: number, b: number) => {
        s.x.set([0, 4, 0]); s.y.set([0, 0, 4]); s.z.fill(z); s.r.fill(r); s.g.fill(0); s.b.fill(b);
        rasterTriangle(target, s, 0, 1, 2);
      };
      for (const which of order) {
        if (which === 'far') tri(0.5, 1, 0);
        else tri(-0.5, 0, 1);
      }
      const img = resolve(target);
      // sample (0, 0) is inside both triangles: blue (near) wins
      expect([img[0], img[2], img[3]]).toEqual([0, 255, 255]);
    }
  });

  it('frames small parts by their drawn geometry: nothing on the border, never empty', () => {
    // consilium N3: framing by toolpath axes cut 0.5-1 mm parts and lost a 0.01 mm one entirely
    for (const len of [250, 2, 1, 0.5, 0.01]) {
      const img = render([layer(0.2, [10, 10, 0.2, 1, 10 + len, 10, 0.2, 0])], { 1: [0.2, 0.8, 0.2] }, 64);
      let border = 0, visible = 0;
      for (let y = 0; y < 64; y++) for (let x = 0; x < 64; x++) {
        if (alphaAt(img, 64, x, y) === 0) continue;
        visible++;
        if (x === 0 || y === 0 || x === 63 || y === 63) border++;
      }
      expect(border, `${len} mm`).toBe(0);
      expect(visible, `${len} mm`).toBeGreaterThan(10);
      expect(isEmpty(img)).toBe(false);
    }
  });

  it('isEmpty sees a fully transparent image', () => {
    expect(isEmpty(new Uint8Array(4 * 16))).toBe(true);
    const one = new Uint8Array(4 * 16);
    one[4 * 7 + 3] = 1;
    expect(isEmpty(one)).toBe(false);
  });

  it('is deterministic', () => {
    const layers = [layer(0.2, [0, 0, 0.2, 1, 10, 3, 0.2, 0], [10, 3, 0.2, 1, 2, 9, 0.2, 0])];
    expect(Buffer.from(render(layers, { 1: [0.3, 0.6, 0.9] })).equals(Buffer.from(render(layers, { 1: [0.3, 0.6, 0.9] })))).toBe(true);
  });
});
