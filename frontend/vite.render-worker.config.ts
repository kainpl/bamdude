import { defineConfig } from 'vite';
import { fileURLToPath } from 'node:url';

const here = fileURLToPath(new URL('.', import.meta.url));

// The part-render page (spec §5.5): self-contained, relative base, its own
// output directory and Vite manifest. It is built AFTER the app, whose
// `emptyOutDir` wipes the whole of static/.
export default defineConfig({
  root: `${here}render-worker`,
  base: './',
  build: {
    outDir: `${here}../static/render-worker`,
    emptyOutDir: true,
    manifest: true,
    chunkSizeWarningLimit: 3000,
    rollupOptions: { input: `${here}render-worker/index.html` },
  },
});
