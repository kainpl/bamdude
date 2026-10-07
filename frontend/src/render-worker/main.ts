import { parseGcodeToolpath } from '../lib/gcodeToolpath';
import { RENDERER_VERSION, RenderError, type RenderJob, type RenderManifest } from './protocol';
import { fitsBox, paletteFromGcode, selectSegments, type Selection } from './select';

export interface RenderDeps {
  fetch: typeof fetch;
  render(selection: Selection, palette: string[], defaultWidth: number, size: number): Promise<Blob>;
  sha256(bytes: ArrayBuffer): Promise<string>;
}

const JSON_HEADERS = { 'Content-Type': 'application/json' };

/** A GET of the attempt's own input; anything but 2xx is the transport failing, never data to parse. */
async function fetchInput(deps: RenderDeps, url: string): Promise<Response> {
  const resp = await deps.fetch(url);
  if (!resp.ok) throw new RenderError('crashed', `${url} answered ${resp.status}`);
  return resp;
}

/** Runs one step and files any failure that is not already a RenderError under `reason`. */
async function step<T>(reason: RenderError['reason'], run: () => Promise<T> | T): Promise<T> {
  try {
    return await run();
  } catch (error) {
    if (error instanceof RenderError) throw error;
    throw new RenderError(reason, String(error));
  }
}

/**
 * One attempt: read the job and the plate, render each object, upload PNGs,
 * then the manifest -- or, on any failure, one `error` post the job server
 * turns into a plate outcome (spec §5.3). Never leaves the server waiting for a
 * deadline when it already knows the answer. Only the parser and the palette
 * answer `parse_failed`; a failing render step or transport is `crashed`.
 */
export async function runJob(deps: RenderDeps): Promise<void> {
  try {
    const job = await step('crashed', async () => (await (await fetchInput(deps, 'job.json')).json()) as RenderJob);
    const gcode = await step('crashed', async () => (await fetchInput(deps, 'plate.gcode')).text());
    const { parsed, palette } = await step('parse_failed', () => ({
      parsed: parseGcodeToolpath(gcode),
      palette: paletteFromGcode(gcode),
    }));
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

      const { bytes, sha256 } = await step('crashed', async () => {
        const blob = await deps.render(selection, palette, parsed.defaultWidth, job.size);
        const raw = await blob.arrayBuffer();
        return { bytes: raw, sha256: await deps.sha256(raw) };
      });
      const put = await step('crashed', () =>
        deps.fetch(`png/${target.id}`, { method: 'POST', body: bytes, headers: { 'Content-Type': 'image/png' } }),
      );
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
    const sent = await step('crashed', () =>
      deps.fetch('manifest', { method: 'POST', body: JSON.stringify(manifest), headers: JSON_HEADERS }),
    );
    // A refused manifest (over the attempt's byte budget) would otherwise leave
    // the server waiting for its deadline.
    if (!sent.ok) throw new RenderError('invalid_output', `manifest refused with ${sent.status}`);
  } catch (error) {
    const reason = error instanceof RenderError ? error.reason : 'crashed';
    await deps
      .fetch('error', { method: 'POST', body: JSON.stringify({ reason, message: String(error) }), headers: JSON_HEADERS })
      .catch(() => undefined);
  }
}
