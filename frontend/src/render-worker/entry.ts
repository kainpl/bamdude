import type * as THREE from 'three';

import { runJob } from './main';
import { createRenderer, renderSelection } from './render';

let renderer: THREE.WebGLRenderer | null = null;

async function sha256(bytes: ArrayBuffer): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  return [...new Uint8Array(digest)].map((b) => b.toString(16).padStart(2, '0')).join('');
}

void runJob({
  fetch: (input, init) => fetch(input, init),
  async render(selection, palette, defaultWidth, size) {
    renderer ??= createRenderer(size);
    return renderSelection(renderer, selection, palette, defaultWidth);
  },
  sha256,
}).finally(() => {
  renderer?.dispose();
});
