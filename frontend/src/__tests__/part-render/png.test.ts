import { describe, it, expect } from 'vitest';
import { inflateSync } from 'node:zlib';
import { crc32, encodePng } from '../../part-render/png';

function chunks(png: Uint8Array) {
  const view = new DataView(png.buffer, png.byteOffset, png.byteLength);
  const out: { type: string; data: Uint8Array; crc: number }[] = [];
  for (let at = 8; at < png.length; ) {
    const len = view.getUint32(at);
    const type = String.fromCharCode(...png.subarray(at + 4, at + 8));
    out.push({ type, data: png.subarray(at + 8, at + 8 + len), crc: view.getUint32(at + 8 + len) });
    at += 12 + len;
  }
  return out;
}

describe('encodePng', () => {
  const rgba = new Uint8Array(3 * 2 * 4).map((_, i) => (i * 37) & 255);
  const png = encodePng(rgba, 3, 2);

  it('writes the signature and IHDR / IDAT / IEND with valid CRCs', () => {
    expect([...png.subarray(0, 8)]).toEqual([137, 80, 78, 71, 13, 10, 26, 10]);
    const cs = chunks(png);
    expect(cs.map((c) => c.type)).toEqual(['IHDR', 'IDAT', 'IEND']);
    for (const c of cs) {
      const typed = new Uint8Array(4 + c.data.length);
      typed.set([...c.type].map((ch) => ch.charCodeAt(0)));
      typed.set(c.data, 4);
      expect(crc32(typed)).toBe(c.crc);
    }
  });

  it('stores the pixels row by row behind filter byte 0', () => {
    const raw = inflateSync(chunks(png)[1].data);
    expect(raw.length).toBe(2 * (3 * 4 + 1));
    expect(raw[0]).toBe(0);
    expect([...raw.subarray(1, 13)]).toEqual([...rgba.subarray(0, 12)]);
    expect([...raw.subarray(14, 26)]).toEqual([...rgba.subarray(12, 24)]);
  });

  it('knows the CRC of "IEND"', () => {
    expect(crc32(new TextEncoder().encode('IEND'))).toBe(0xae426082);
  });

  it('refuses a buffer of the wrong size', () => {
    expect(() => encodePng(new Uint8Array(5), 3, 2)).toThrow(RangeError);
  });
});
