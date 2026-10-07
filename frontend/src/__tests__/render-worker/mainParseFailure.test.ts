import { describe, it, expect, vi } from 'vitest';

import { runJob, type RenderDeps } from '../../render-worker/main';

// The parser itself throwing is the one exception that is deterministic for
// the same bytes: spec §5.3 settles it as parse_failed, without retries.
vi.mock('../../lib/gcodeToolpath', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/gcodeToolpath')>()),
  parseGcodeToolpath: () => {
    throw new Error('unparseable');
  },
}));

describe('runJob with a parser that throws', () => {
  it('posts parse_failed', async () => {
    const posts: Array<{ url: string; body: unknown }> = [];
    const deps: RenderDeps = {
      fetch: vi.fn(async (url: string, init?: RequestInit) => {
        if (init?.method === 'POST') {
          posts.push({ url, body: JSON.parse(String(init.body)) });
          return new Response(null, { status: 204 });
        }
        if (url === 'job.json') return new Response(JSON.stringify({ size: 512, objects: [{ id: 1, mode: 'toolpath' }] }));
        return new Response('G1 X1 E1\n');
      }) as unknown as typeof fetch,
      render: vi.fn(),
      sha256: vi.fn(),
    };
    await runJob(deps);
    expect(deps.render).not.toHaveBeenCalled();
    expect(posts).toEqual([{ url: 'error', body: expect.objectContaining({ reason: 'parse_failed' }) }]);
  });
});
