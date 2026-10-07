// Builds the part-render bundle for Node (spec §5.5), refuses it when its dependency
// guard fails (consilium N5), and records size, sha256, imports and source modules.
// rolldown is the bundler under Vite 8; platform 'node' leaves node: built-ins external.
import { build } from 'rolldown';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { checkChunk, checkCode } from './part-render-guard.mjs';

const frontend = dirname(dirname(fileURLToPath(import.meta.url)));
const outDir = join(frontend, '..', 'backend', 'app', 'data', 'part_render');
const file = join(outDir, 'part-render.mjs');
mkdirSync(outDir, { recursive: true });
const { output } = await build({
  input: join(frontend, 'src', 'part-render', 'entry.ts'),
  platform: 'node',
  output: { file, format: 'esm' },
  logLevel: 'warn',
});
const chunk = output.find((o) => o.type === 'chunk' && o.isEntry);
const code = readFileSync(file, 'utf8');
const problems = [...checkChunk(chunk), ...checkCode(code)];
if (problems.length > 0) {
  console.error(`part-render bundle refused by its guard:\n  ${problems.join('\n  ')}`);
  process.exit(1);
}
const bytes = readFileSync(file);
const manifest = {
  file: 'part-render.mjs',
  bytes: bytes.length,
  sha256: createHash('sha256').update(bytes).digest('hex'),
  imports: [...chunk.imports].sort(),
  modules: chunk.moduleIds.map((id) => relative(frontend, id).replaceAll('\\', '/')).sort(),
};
writeFileSync(join(outDir, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n');
console.log(`part-render.mjs ${bytes.length} bytes ${manifest.sha256}`);
