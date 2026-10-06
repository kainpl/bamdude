import { describe, it, expect } from 'vitest';
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { parseGcodeToolpath, type ParsedToolpath } from '../../lib/gcodeToolpath';

// Canonical fixtures ship with the backend (the render probe uses them on a
// field install); the frontend reads the same files.
const FIXTURES = join(process.cwd(), '..', 'backend', 'app', 'data', 'render_probe');
const load = (name: string) => readFileSync(join(FIXTURES, name), 'utf8');

/** What the renderer consumes: z, stride-8 records and widths -- never the ownership metadata. */
function geometryDigest(parsed: ParsedToolpath): string {
  const hash = createHash('sha256');
  for (const layer of parsed.layers) {
    hash.update(String(layer.z));
    hash.update(new Uint8Array(layer.paths.buffer, layer.paths.byteOffset, layer.paths.byteLength));
    hash.update(JSON.stringify(layer.widths));
  }
  return hash.digest('hex');
}

// Pinned on f1ad4f1e2, before the parser learned object ownership (spec §5.1).
// Metadata must never move a coordinate, a type, a width or a layer: if one of
// these changes, the parser change is wrong, not the pin.
const PINNED: Record<string, { digest: string; segments: number; travels: number; layers: number }> = {
  'two-objects.gcode': {
    digest: 'cdc7056f4d51b6a72e737268c2b0b35d51ee4e161eccac03e3871003c323933c',
    segments: 86,
    travels: 9,
    layers: 3,
  },
  'single-no-markers.gcode': {
    digest: '483d975db802a12f92cb906c49e3f1c340756fe8d95f138619d88bed581034b1',
    segments: 10,
    travels: 3,
    layers: 3,
  },
};

describe('toolpath geometry is pinned', () => {
  for (const [name, expected] of Object.entries(PINNED)) {
    it(`${name} parses to the pinned geometry`, () => {
      const parsed = parseGcodeToolpath(load(name));
      expect(parsed.segmentCount).toBe(expected.segments);
      expect(parsed.travelCount).toBe(expected.travels);
      expect(parsed.layers).toHaveLength(expected.layers);
      expect(geometryDigest(parsed)).toBe(expected.digest);
    });

    it(`${name} parses to the same geometry with CRLF line endings`, () => {
      const parsed = parseGcodeToolpath(load(name).replaceAll('\r\n', '\n').replaceAll('\n', '\r\n'));
      expect(geometryDigest(parsed)).toBe(expected.digest);
    });
  }
});
