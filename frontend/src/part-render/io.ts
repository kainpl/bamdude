/**
 * stdin/stdout protocol of the part-render script (spec §5.6). stdin: one JSON line (the
 * RenderJob), then the plate's G-code. stdout: frames `kind(1) | length(u32 BE) | payload`:
 *   'P' -- id (u32 BE) + PNG bytes, one per rendered object, in job order
 *   'M' -- the manifest JSON, last
 *   'E' -- {reason, message} JSON instead of 'M' when the job fails
 * Exit code 0 with 'M', 1 with 'E'. No file system, network or child process.
 */
import { runJob } from './job';
import { MANIFEST_MAX, RenderError, type RenderErrorReason, type RenderJob } from './protocol';

function frame(kind: 'P' | 'M' | 'E', payload: Uint8Array): Uint8Array {
  const out = new Uint8Array(5 + payload.length);
  out[0] = kind.charCodeAt(0);
  new DataView(out.buffer).setUint32(1, payload.length);
  out.set(payload, 5);
  return out;
}

const encoder = new TextEncoder();

async function readAll(stream: NodeJS.ReadableStream): Promise<Uint8Array> {
  const chunks: Buffer[] = [];
  for await (const chunk of stream) chunks.push(chunk as Buffer);
  return Buffer.concat(chunks);
}

function write(stream: NodeJS.WritableStream, bytes: Uint8Array): Promise<void> {
  return new Promise((done, fail) => stream.write(bytes, (error) => (error ? fail(error) : done())));
}

/** A typed-array allocation the heap could not satisfy is a memory limit, not a crash (consilium N6). */
/** By name, not instanceof: the engine's RangeError can come from another realm (vitest's jsdom environment). */
function isRangeError(error: unknown): error is Error {
  return typeof error === 'object' && error !== null && (error as Error).name === 'RangeError';
}

function reasonOf(error: unknown): RenderErrorReason {
  if (error instanceof RenderError) return error.reason;
  if (isRangeError(error) && /allocation failed|invalid (typed )?array length/i.test(String(error.message))) return 'memory_limit';
  return 'crashed';
}

export async function main(stdin: NodeJS.ReadableStream, stdout: NodeJS.WritableStream): Promise<number> {
  const pending: Promise<void>[] = [];
  try {
    const input = await readAll(stdin);
    const newline = input.indexOf(10);
    if (newline < 0) throw new RenderError('invalid_output', 'no job line on stdin');
    let job: RenderJob;
    try {
      job = JSON.parse(new TextDecoder().decode(input.subarray(0, newline))) as RenderJob;
    } catch (error) {
      throw new RenderError('invalid_output', `unreadable job line: ${String(error)}`);
    }
    // fatal: false -- a stray non-UTF-8 byte in a comment must not fail the plate
    const gcode = new TextDecoder('utf-8', { fatal: false }).decode(input.subarray(newline + 1));
    const manifest = runJob(job, gcode, (id, png) => {
      const payload = new Uint8Array(4 + png.length);
      new DataView(payload.buffer).setUint32(0, id);
      payload.set(png, 4);
      pending.push(write(stdout, frame('P', payload)));
    });
    await Promise.all(pending);
    const body = encoder.encode(JSON.stringify(manifest));
    if (body.length > MANIFEST_MAX) throw new RenderError('invalid_output', `manifest of ${body.length} bytes exceeds ${MANIFEST_MAX}`);
    await write(stdout, frame('M', body));
    return 0;
  } catch (error) {
    await Promise.allSettled(pending);
    const reason = reasonOf(error);
    await write(stdout, frame('E', encoder.encode(JSON.stringify({ reason, message: String(error).slice(0, 500) }))));
    return 1;
  }
}
