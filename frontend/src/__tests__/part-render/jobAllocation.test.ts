import { describe, it, expect, vi } from 'vitest';

// The parser itself runs out of memory: a ceiling-size plate that the heap cannot hold.
vi.mock('../../lib/gcodeToolpath', async (importOriginal) => ({
  ...(await importOriginal<typeof import('../../lib/gcodeToolpath')>()),
  parseGcodeToolpath: () => {
    throw new RangeError('Array buffer allocation failed');
  },
}));

import { runJob } from '../../part-render/job';
import type { RenderError } from '../../part-render/protocol';

describe('runJob', () => {
  it('reports an allocation the parser cannot get as memory_limit, not parse_failed', () => {
    let reason = 'no error';
    try {
      runJob({ size: 8, objects: [{ id: 1, mode: 'toolpath' }], outputBytes: 8 * 2 ** 20 }, '; plate', () => {});
    } catch (error) {
      reason = (error as RenderError).reason;
    }
    expect(reason).toBe('memory_limit');
  });
});
