import { parseGcodeToolpath } from '../lib/gcodeToolpath';
import { RENDERER_VERSION, RenderError, type RenderJob, type RenderManifest } from './protocol';
import { fitsBox, paletteFromGcode, selectSegments, type Selection } from './select';

export interface RenderDeps {
  fetch: typeof fetch;
  render(selection: Selection, palette: string[], defaultWidth: number, size: number): Promise<Blob>;
  sha256(bytes: ArrayBuffer): Promise<string>;
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

/**
 * One attempt: read the job and the plate, render each object, upload PNGs,
 * then the manifest -- or, on any failure, one `error` post the job server
 * turns into a plate outcome (spec §5.3). Never leaves the server waiting for a
 * deadline when it already knows the answer.
 */
export async function runJob(deps: RenderDeps): Promise<void> {
  try {
    const job = (await (await deps.fetch('job.json')).json()) as RenderJob;
    const gcode = await (await deps.fetch('plate.gcode')).text();
    const parsed = parseGcodeToolpath(gcode);
    const palette = paletteFromGcode(gcode);
    const objects: RenderManifest['objects'] = [];

    for (const target of job.objects) {
      const selection = selectSegments(parsed, target.mode === 'model' ? { kind: 'model' } : { kind: 'object', id: target.id });
      if (selection.segments === 0 || selection.bounds === null) {
        objects.push({ id: target.id, method: 'missing', reason: 'empty_selection' });
        continue;
      }
      if (target.mode === 'model' && !(target.bbox && fitsBox(selection.bounds, target.bbox))) {
        objects.push({ id: target.id, method: 'missing', reason: 'model_unproven' });
        continue;
      }
      const tools = Object.keys(selection.tools).map(Number).sort((a, b) => a - b);
      const uncoloured = tools.filter((t) => !palette[t]);
      if (uncoloured.length > 0) {
        throw new RenderError('parse_failed', `no filament colour for T${uncoloured.join(', T')}`);
      }

      const blob = await deps.render(selection, palette, parsed.defaultWidth, job.size);
      const bytes = await blob.arrayBuffer();
      const sha256 = await deps.sha256(bytes);
      const put = await deps.fetch(`png/${target.id}`, { method: 'POST', body: bytes, headers: { 'Content-Type': 'image/png' } });
      if (!put.ok) throw new RenderError('invalid_output', `PNG upload refused with ${put.status}`);
      objects.push({
        id: target.id,
        method: target.mode,
        width: job.size,
        height: job.size,
        tools,
        bounds: selection.bounds,
        segments: selection.segments,
        sha256,
        bytes: bytes.byteLength,
      });
    }

    const manifest: RenderManifest = { renderer: RENDERER_VERSION, palette, objects };
    await deps.fetch('manifest', { method: 'POST', body: JSON.stringify(manifest), headers: JSON_HEADERS });
  } catch (error) {
    const reason = error instanceof RenderError ? error.reason : 'parse_failed';
    await deps
      .fetch('error', { method: 'POST', body: JSON.stringify({ reason, message: String(error) }), headers: JSON_HEADERS })
      .catch(() => undefined);
  }
}
