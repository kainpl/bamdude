import { describe, it, expect } from 'vitest';
import { Readable, Writable } from 'node:stream';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { main } from '../../part-render/io';

const FIXTURES = join(process.cwd(), '..', 'backend', 'app', 'data', 'render_probe');

async function run(input: Uint8Array) {
  const out: Buffer[] = [];
  const stdout = new Writable({ write(chunk, _enc, done) { out.push(Buffer.from(chunk)); done(); } });
  const code = await main(Readable.from([Buffer.from(input)]), stdout);
  const bytes = Buffer.concat(out);
  const frames: { kind: string; payload: Buffer }[] = [];
  for (let at = 0; at < bytes.length; ) {
    const len = bytes.readUInt32BE(at + 1);
    frames.push({ kind: String.fromCharCode(bytes[at]), payload: bytes.subarray(at + 5, at + 5 + len) });
    at += 5 + len;
  }
  return { code, frames };
}
const input = (jobLine: string, body: Uint8Array) => Buffer.concat([Buffer.from(jobLine + '\n'), Buffer.from(body)]);
const reason = (f: { payload: Buffer }) => JSON.parse(f.payload.toString()).reason;

describe('main', () => {
  const plate = readFileSync(join(FIXTURES, 'two-objects.gcode'));
  const jobLine = JSON.stringify({ size: 32, outputBytes: 8 * 2 ** 20, objects: [{ id: 101, mode: 'toolpath' }, { id: 202, mode: 'toolpath' }] });

  it('frames PNGs in job order and the manifest last', async () => {
    const { code, frames } = await run(input(jobLine, plate));
    expect(code).toBe(0);
    expect(frames.map((f) => f.kind)).toEqual(['P', 'P', 'M']);
    expect(frames[0].payload.readUInt32BE(0)).toBe(101);
    expect([...frames[0].payload.subarray(4, 8)]).toEqual([137, 80, 78, 71]);
    expect(JSON.parse(frames[2].payload.toString()).objects).toHaveLength(2);
  });

  it('decodes a G-code with stray non-UTF-8 bytes', async () => {
    const dirty = Buffer.concat([Buffer.from('; object name: '), Buffer.from([0xcf, 0xf0, 0xe8]), Buffer.from('\n'), plate]);
    const { code, frames } = await run(input(jobLine, dirty));
    expect(code).toBe(0);
    expect(frames.at(-1)!.kind).toBe('M');
  });

  it('answers an unreadable job line with an E frame', async () => {
    const { code, frames } = await run(input('{not json', plate));
    expect(code).toBe(1);
    expect(frames.map((f) => f.kind)).toEqual(['E']);
    expect(reason(frames[0])).toBe('invalid_output');
  });

  it('answers a missing job line with an E frame', async () => {
    const { code, frames } = await run(Buffer.from('no newline at all'));
    expect(code).toBe(1);
    expect(reason(frames[0])).toBe('invalid_output');
  });

  it('reports an allocation the heap cannot satisfy as memory_limit', async () => {
    const huge = JSON.stringify({ size: 1_000_000, outputBytes: 8 * 2 ** 20, objects: [{ id: 101, mode: 'toolpath' }] });
    const { code, frames } = await run(input(huge, plate));
    expect(code).toBe(1);
    expect(reason(frames[0])).toBe('memory_limit');
  });
});
