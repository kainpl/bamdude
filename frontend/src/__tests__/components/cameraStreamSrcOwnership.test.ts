import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join } from 'node:path';

/**
 * A camera stream URL never reaches an element as a render-time prop.
 *
 * An MJPEG <img> starts loading the moment it gets its URL. React sets props
 * while it renders — also for a render it then throws away — so `src={streamUrl}`
 * opens a stream on an image that never reaches the page and that nothing can
 * close. The URL is assigned by `hooks/useCameraImageRef` from the ref callback
 * of a mounted node, which also clears it when the node goes (upstream #3431).
 *
 * The scan follows the URL by name: every `const` whose initialiser builds a
 * stream URL (`streamPath(`, `getCameraStreamUrl(`, a hand-written
 * `/camera/stream` path) or is derived from such a name is a stream URL, and a
 * JSX `src={…}` that mentions one fails.
 */
const SRC = join(process.cwd(), 'src');
const BUILDS_STREAM_URL = /streamPath\(|getCameraStreamUrl\(|\/camera\/stream|\/cameras\/\$\{[^}]*\}\/stream/;

function sources(dir: string): string[] {
  return readdirSync(join(SRC, dir)).flatMap((name) => {
    const rel = dir ? `${dir}/${name}` : name;
    if (statSync(join(SRC, rel)).isDirectory()) return name === '__tests__' ? [] : sources(rel);
    return name.endsWith('.tsx') ? [rel] : [];
  });
}

export function streamSrcProps(text: string): string[] {
  const declarations = [...text.matchAll(/\b(?:const|let)\s+(\w+)\s*=([^;]*);/g)].map((match) => ({
    name: match[1],
    body: match[2],
  }));
  const streamNames = new Set<string>();
  const mentionsStream = (code: string) =>
    BUILDS_STREAM_URL.test(code) || [...streamNames].some((name) => new RegExp(`\\b${name}\\b`).test(code));
  for (let grew = true; grew; ) {
    grew = false;
    for (const { name, body } of declarations) {
      if (!streamNames.has(name) && mentionsStream(body)) {
        streamNames.add(name);
        grew = true;
      }
    }
  }
  // The rest of the line, not "up to the brace": a template literal has braces of its own.
  return [...text.matchAll(/\bsrc=\{(.*)/g)].filter((match) => mentionsStream(match[1])).map((match) => match[0].trim());
}

describe('camera stream URLs', () => {
  it('finds a stream URL passed as a prop, directly or through a name', () => {
    expect(streamSrcProps('const a = streamPath(s, 8);\nconst b = withToken(a);\n<img src={b} />')).toEqual(['src={b} />']);
    expect(streamSrcProps('<img src={`/api/v1/printers/${id}/camera/stream?fps=5`} />')).toHaveLength(1);
    expect(streamSrcProps('const a = streamPath(s, 8);\n<img ref={attach} alt="x" />\n<img src={logo} />')).toEqual([]);
  });

  it('are never a render-time src prop', () => {
    const offenders = sources('').flatMap((rel) =>
      streamSrcProps(readFileSync(join(SRC, rel), 'utf8')).map((prop) => `${rel}: ${prop}`),
    );
    expect(offenders).toEqual([]);
  });
});
