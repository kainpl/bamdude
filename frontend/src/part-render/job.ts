/**
 * One render job (spec §5.2-5.3, §5.6): parse the plate once, then for each
 * requested id select its segments, frame, rasterize, encode. Pure apart from
 * hashing -- the Node entry feeds it stdin and frames its output.
 */
import { createHash } from 'node:crypto';
import { parseGcodeToolpath } from '../lib/gcodeToolpath';
import { buildSegmentData } from '../lib/vendor/toolpathRenderer.js';
import {
  CONTROL_FRAME_MAX, PNG_FRAME_OVERHEAD, RENDERER_VERSION, RenderError, isAllocationFailure, type RenderJob, type RenderManifest,
} from './protocol';
import { drawSegments, frameCamera, geometryBounds, isEmpty, RenderTarget, resolve } from './raster';
import { encodePng } from './png';
import { fitsBox, paletteFromGcode, selectSegments } from './select';

export const DEFAULT_SUPERSAMPLE = 2;

function parseHex(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1, 7), 16);
  return [((n >> 16) & 0xff) / 255, ((n >> 8) & 0xff) / 255, (n & 0xff) / 255];
}

/**
 * Calls `emit(id, png)` for every rendered object, in job order, and returns the manifest.
 * `draw` is the rasterizer; tests pass another one to reach the empty-render branch.
 */
export function runJob(
  job: RenderJob,
  gcode: string,
  emit: (id: number, png: Uint8Array) => void,
  draw: typeof drawSegments = drawSegments,
): RenderManifest {
  let parsed, palette: string[];
  try {
    parsed = parseGcodeToolpath(gcode);
    palette = paletteFromGcode(gcode);
  } catch (error) {
    // A plate the heap cannot hold is a memory limit (fallback methods), not a deterministic parse failure.
    throw new RenderError(isAllocationFailure(error) ? 'memory_limit' : 'parse_failed', String(error));
  }
  const ss = job.supersample ?? DEFAULT_SUPERSAMPLE;
  const target = new RenderTarget(job.size, ss);
  const objects: RenderManifest['objects'] = [];
  let spent = 0;

  for (const want of job.objects) {
    const selection = selectSegments(parsed, want.mode === 'model' ? { kind: 'model' } : { kind: 'object', id: want.id });
    if (selection.segments === 0 || selection.bounds === null) {
      objects.push({ id: want.id, method: 'missing', reason: 'empty_selection' });
      continue;
    }
    if (want.mode === 'model' && !(want.bbox && fitsBox(selection.bounds, want.bbox))) {
      objects.push({ id: want.id, method: 'missing', reason: 'model_unproven' });
      continue;
    }
    const tools = Object.keys(selection.tools).map(Number).sort((a, b) => a - b);
    const uncoloured = tools.filter((t) => !palette[t]);
    if (uncoloured.length > 0) throw new RenderError('parse_failed', `no filament colour for T${uncoloured.join(', T')}`);

    const data = buildSegmentData(selection.layers, parsed.defaultWidth);
    if (data.hasNaN) throw new RenderError('parse_failed', 'segment geometry contains NaN');
    // vType is tool + 1 after selectSegments re-keyed it (spec §5.2)
    const rgb = new Float32Array(data.nV * 3);
    for (let v = 0; v < data.nV; v++) {
      const [r, g, b] = parseHex(palette[data.meta.vType[v] - 1]);
      rgb[v * 3] = r;
      rgb[v * 3 + 1] = g;
      rgb[v * 3 + 2] = b;
    }
    const drawn = geometryBounds(data);
    if (drawn === null) {
      objects.push({ id: want.id, method: 'missing', reason: 'empty_selection' });
      continue;
    }
    target.clear();
    draw(data, rgb, frameCamera(drawn), target);
    const rgba = resolve(target);
    if (isEmpty(rgba)) {
      // a fully transparent picture is not a toolpath render (consilium N3)
      objects.push({ id: want.id, method: 'missing', reason: 'empty_render' });
      continue;
    }
    const png = encodePng(rgba, job.size, job.size);
    spent += png.length + PNG_FRAME_OVERHEAD;
    if (spent + CONTROL_FRAME_MAX > job.outputBytes) {
      throw new RenderError('invalid_output', `PNGs exceed the attempt budget of ${job.outputBytes} bytes`);
    }
    emit(want.id, png);
    objects.push({
      id: want.id,
      method: want.mode,
      width: job.size,
      height: job.size,
      tools,
      bounds: selection.bounds,
      segments: selection.segments,
      sha256: createHash('sha256').update(png).digest('hex'),
      bytes: png.length,
    });
  }
  return { renderer: RENDERER_VERSION, palette, objects };
}
