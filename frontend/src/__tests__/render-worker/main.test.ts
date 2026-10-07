import { describe, it, expect, vi } from 'vitest';
import { Blob as NodeBlob } from 'node:buffer';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { runJob, type RenderDeps } from '../../render-worker/main';
import { RENDERER_VERSION, type RenderJob, type RenderManifest } from '../../render-worker/protocol';

const FIXTURES = join(process.cwd(), '..', 'backend', 'app', 'data', 'render_probe');
const load = (name: string) => readFileSync(join(FIXTURES, name), 'utf8');

function harness(
  job: RenderJob,
  gcode: string,
  opts: { pngStatus?: number; plateStatus?: number; manifestStatus?: number } = {},
) {
  const posts: Array<{ url: string; body: unknown }> = [];
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    if (!init || init.method !== 'POST') {
      if (url === 'job.json') return new Response(JSON.stringify(job));
      if (url === 'plate.gcode') return new Response(gcode, { status: opts.plateStatus ?? 200 });
      return new Response('', { status: 404 });
    }
    const body = typeof init.body === 'string' ? JSON.parse(init.body) : init.body;
    posts.push({ url, body });
    if (url.startsWith('png/')) return new Response(null, { status: opts.pngStatus ?? 204 });
    if (url === 'manifest') return new Response(null, { status: opts.manifestStatus ?? 204 });
    return new Response(null, { status: 204 });
  });
  const deps: RenderDeps = {
    fetch: fetchImpl as unknown as typeof fetch,
    // jsdom's Blob has no arrayBuffer(); the page's real Blob does, as node:buffer's does.
    render: vi.fn(async () => new NodeBlob([new Uint8Array([137, 80, 78, 71, 1, 2, 3])], { type: 'image/png' }) as unknown as Blob),
    sha256: vi.fn(async () => 'ab'.repeat(32)),
  };
  return { deps, posts };
}

describe('runJob', () => {
  it('uploads a PNG per object and then the manifest', async () => {
    const { deps, posts } = harness(
      { size: 512, objects: [{ id: 101, mode: 'toolpath' }, { id: 202, mode: 'toolpath' }] },
      load('two-objects.gcode'),
    );
    await runJob(deps);
    expect(posts.map((p) => p.url)).toEqual(['png/101', 'png/202', 'manifest']);
    const manifest = posts[2].body as RenderManifest;
    expect(manifest.renderer).toBe(RENDERER_VERSION);
    expect(manifest.palette).toEqual(['#C0C0C0', '#161616', '#00AE42']);
    expect(manifest.objects[0]).toMatchObject({ id: 101, method: 'toolpath', tools: [0, 2], segments: 7, bytes: 7, width: 512 });
  });

  it('runJob reports an id without segments as missing', async () => {
    const { deps, posts } = harness({ size: 512, objects: [{ id: 999, mode: 'toolpath' }] }, load('two-objects.gcode'));
    await runJob(deps);
    expect(deps.render).not.toHaveBeenCalled();
    expect((posts[0].body as RenderManifest).objects).toEqual([{ id: 999, method: 'missing', reason: 'empty_selection' }]);
  });

  it('refuses a model render whose bounds leave the object box', async () => {
    const { deps, posts } = harness(
      { size: 512, objects: [{ id: 1, mode: 'model', bbox: [0, 0, 10, 10] }] },
      load('single-no-markers.gcode'),
    );
    await runJob(deps);
    expect((posts[0].body as RenderManifest).objects).toEqual([{ id: 1, method: 'missing', reason: 'model_unproven' }]);
  });

  it('refuses a model render without an object box', async () => {
    const { deps, posts } = harness({ size: 512, objects: [{ id: 1, mode: 'model' }] }, load('single-no-markers.gcode'));
    await runJob(deps);
    expect((posts[0].body as RenderManifest).objects[0]).toMatchObject({ method: 'missing', reason: 'model_unproven' });
  });

  it('renders a proven model area', async () => {
    const { deps, posts } = harness(
      { size: 512, objects: [{ id: 1, mode: 'model', bbox: [119, 119, 126, 126] }] },
      load('single-no-markers.gcode'),
    );
    await runJob(deps);
    expect(posts.map((p) => p.url)).toEqual(['png/1', 'manifest']);
    expect((posts[1].body as RenderManifest).objects[0]).toMatchObject({ method: 'model', segments: 8 });
  });

  it('posts parse_failed when a used filament has no colour', async () => {
    const gcode = load('two-objects.gcode').replace('; filament_colour = #C0C0C0;#161616;#00AE42', '; filament_colour = #C0C0C0');
    const { deps, posts } = harness({ size: 512, objects: [{ id: 101, mode: 'toolpath' }] }, gcode);
    await runJob(deps);
    expect(posts.map((p) => p.url)).toEqual(['error']);
    expect(posts[0].body).toMatchObject({ reason: 'parse_failed' });
  });

  it('runJob posts error when rendering throws', async () => {
    // A WebGL context that cannot be made, a GPU process that died, a canvas
    // that gives no blob: transient (spec §5.3 "падіння"), never parse_failed,
    // which would settle the plate for good.
    const { deps, posts } = harness({ size: 512, objects: [{ id: 101, mode: 'toolpath' }] }, load('two-objects.gcode'));
    deps.render = vi.fn(async () => {
      throw new Error('boom');
    });
    await runJob(deps);
    expect(posts.map((p) => p.url)).toEqual(['error']);
    expect(posts[0].body).toMatchObject({ reason: 'crashed' });
  });

  it('a plate the server refuses is crashed and never parsed', async () => {
    const { deps, posts } = harness({ size: 512, objects: [{ id: 101, mode: 'toolpath' }] }, 'G1 X1 E1\n', { plateStatus: 503 });
    await runJob(deps);
    expect(deps.render).not.toHaveBeenCalled();
    expect(posts.map((p) => p.url)).toEqual(['error']);
    expect(posts[0].body).toMatchObject({ reason: 'crashed' });
  });

  it('a refused manifest is reported as invalid_output', async () => {
    const { deps, posts } = harness({ size: 512, objects: [{ id: 101, mode: 'toolpath' }] }, load('two-objects.gcode'), {
      manifestStatus: 413,
    });
    await runJob(deps);
    expect(posts.map((p) => p.url)).toEqual(['png/101', 'manifest', 'error']);
    expect(posts[2].body).toMatchObject({ reason: 'invalid_output' });
  });

  it('runJob posts error when the job cannot be read', async () => {
    const { deps, posts } = harness({ size: 512, objects: [] }, '');
    deps.fetch = vi.fn(async (url: string, init?: RequestInit) => {
      if (init?.method === 'POST') {
        posts.push({ url, body: JSON.parse(String(init.body)) });
        return new Response(null, { status: 204 });
      }
      return new Response('not json');
    }) as unknown as typeof fetch;
    await runJob(deps);
    expect(posts.map((p) => p.url)).toEqual(['error']);
    expect(posts[0].body).toMatchObject({ reason: 'crashed' });
  });

  it('treats a refused PNG upload as invalid_output', async () => {
    const { deps, posts } = harness({ size: 512, objects: [{ id: 101, mode: 'toolpath' }] }, load('two-objects.gcode'), { pngStatus: 413 });
    await runJob(deps);
    expect(posts.map((p) => p.url)).toEqual(['png/101', 'error']);
    expect(posts[1].body).toMatchObject({ reason: 'invalid_output' });
  });
});
