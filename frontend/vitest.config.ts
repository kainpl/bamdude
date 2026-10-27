import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import os from 'node:os';
import path from 'path';

// One worker per physical core on a developer machine, not vitest's default of one per
// logical CPU minus one. With hyper-threading that default oversubscribes the cores: on a
// 6-core / 12-thread workstation (measured 2026-10-27) 11 jsdom workers made the same tests
// 2-25x slower than alone, and whichever one crossed its default budget that run failed —
// a page's first render past `findBy`'s 1 s, a synchronous source scan past the 5 s test
// timeout. v0.7.2 failed the same way; 6 workers passed every file. CI keeps the default:
// its runners have no spare threads to oversubscribe and pass as they are.
const localWorkers = Math.max(1, Math.floor(os.availableParallelism() / 2));

export default defineConfig({
  plugins: [react()],
  test: {
    maxWorkers: process.env.CI ? undefined : localWorkers,
    globals: true,
    environment: 'jsdom',
    environmentOptions: {
      jsdom: {
        url: 'http://localhost:3000',
      },
    },
    setupFiles: ['./src/__tests__/setup.ts'],
    include: ['src/**/*.{test,spec}.{js,mjs,cjs,ts,mts,cts,jsx,tsx}'],
    exclude: ['node_modules', 'dist'],
    coverage: {
      provider: 'v8',
      reporter: ['text', 'json', 'html'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/**/*.spec.{ts,tsx}',
        'src/__tests__/**',
        'src/main.tsx',
        'src/vite-env.d.ts',
      ],
    },
  },
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
});
