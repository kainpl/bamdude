import { describe, it, expect } from 'vitest';
import { readFileSync } from 'node:fs';
import { join } from 'node:path';
import { checkChunk, checkCode } from '../../../scripts/part-render-guard.mjs';

const BUNDLE = join(process.cwd(), '..', 'backend', 'app', 'data', 'part_render', 'part-render.mjs');

describe('part-render guard', () => {
  it('passes the tracked bundle', () => {
    expect(checkCode(readFileSync(BUNDLE, 'utf8'))).toEqual([]);
  });

  it.each([
    ['dynamic import', 'const m = await import("node:net");', 'dynamic import()'],
    ['bare builtin import', 'import net from "net"; net.connect(80);', 'import net'],
    ['fetch', 'fetch("http://example.com");', 'global fetch'],
    ['getBuiltinModule', 'process.getBuiltinModule("http").get("http://x");', 'member getBuiltinModule'],
    ['require', 'const h = require("http");', 'global require'],
    ['globalThis.fetch', 'globalThis.fetch("http://x");', 'member fetch'],
    ['eval', 'eval("1");', 'global eval'],
    // consilium R5.2: a global read as a property of a global object, or destructured
    ['globalThis.WebSocket', 'new globalThis.WebSocket("ws://x");', 'member WebSocket'],
    ['global.eval', 'global.eval("1");', 'member eval'],
    ['getBuiltinModule destructured from process', 'const {getBuiltinModule} = process; getBuiltinModule("http");', 'destructured getBuiltinModule'],
    ['fetch destructured under another name', 'const {fetch: request} = globalThis; request("http://x");', 'destructured fetch'],
    ['a string key destructured', 'const {"WebSocket": W} = globalThis; new W("ws://x");', 'destructured WebSocket'],
    ['a literal computed global property', 'globalThis["fetch"]("http://x");', 'member fetch'],
    ['a literal computed member', 'process["getBuiltinModule"]("http");', 'member getBuiltinModule'],
  ])('catches %s', (_name, code, problem) => {
    expect(checkCode(code)).toContain(problem);
  });

  it('leaves allowed code and same-named keys alone', () => {
    expect(checkCode('import { deflateSync } from "node:zlib"; const o = { fetch: 1 }; o.size = deflateSync.length;')).toEqual([]);
    expect(checkCode('const { size, bytes: length } = { size: 1, bytes: 2 }; const t = { WebSocket: size + length };')).toEqual([]);
  });

  it('checks the module graph', () => {
    expect(checkChunk({
      imports: ['node:zlib', 'node:net'],
      dynamicImports: ['x'],
      moduleIds: ['/r/frontend/src/part-render/job.ts', '/r/node_modules/three/build/three.module.js'],
    })).toEqual(['import node:net', 'dynamic import x', 'module /r/node_modules/three/build/three.module.js']);
  });
});
