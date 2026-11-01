/** Build-only client artifacts from canonical sources; never reads credentials. */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { dirname, join, resolve, relative, sep } from 'node:path';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { createHash } from 'node:crypto';
import { createRequire } from 'node:module';

const here = dirname(fileURLToPath(import.meta.url));
const root = resolve(here, '../..');
const adapter = join(root, 'integrations/mcp-adapter');
const require = createRequire(join(adapter, 'package.json'));
const { build } = require('esbuild');
const { zipSync } = require('fflate');
const { MANIFEST_SCHEMAS } = require('@anthropic-ai/mcpb');
const hash = bytes => createHash('sha256').update(bytes).digest('hex');
const json = value => Buffer.from(JSON.stringify(value, null, 2) + '\n');

async function notices(inputs) {
  const dependencies = new Map();
  for (const input of Object.keys(inputs).filter(name => name.includes('node_modules'))) {
    let directory = dirname(resolve(adapter, input));
    while (directory.startsWith(adapter + sep) && directory !== adapter) {
      try {
        const pkg = JSON.parse(await readFile(join(directory, 'package.json'), 'utf8'));
        if (pkg.name && pkg.version) {
          const key = `${pkg.name}@${pkg.version}`;
          if (!dependencies.has(key)) {
            let text;
            for (const name of ['LICENSE', 'LICENSE.md', 'LICENSE.txt', 'license', 'license.md']) {
              try { text = await readFile(join(directory, name), 'utf8'); break; } catch { /* next spelling */ }
            }
            if (!text) throw new Error(`Missing dependency licence: ${key}`);
            dependencies.set(key, { name: pkg.name, version: pkg.version, license: pkg.license, text });
          }
          break;
        }
      } catch (error) { if (error.message.startsWith('Missing dependency licence')) throw error; }
      directory = dirname(directory);
    }
  }
  return json([...dependencies.values()].sort((a, b) => a.name.localeCompare(b.name)));
}

export async function buildPackages(output) {
  const version = (await readFile(join(root, 'backend/app/core/config.py'), 'utf8')).match(/^APP_VERSION = "([^"]+)"/m)?.[1];
  if (!version || !/^[0-9]+\.[0-9]+\.[0-9]+(?:[-+][\w.-]+)?$/.test(version)) throw new Error('Invalid application version');
  const skillRoot = join(root, 'integrations/agent-skills/bamdude-diagnostics');
  const skill = await readFile(join(skillRoot, 'SKILL.md'));
  const guide = await readFile(join(skillRoot, 'references/diagnostics.md'));
  const bundled = await build({ entryPoints: ['src/index.mjs'], absWorkingDir: adapter, bundle: true,
    platform: 'node', target: 'node22', format: 'cjs', write: false, metafile: true, logLevel: 'silent',
    define: { 'process.env.NODE_ENV': '"production"' } });
  const common = {
    'README.md': await readFile(join(here, 'README.package.md')),
    'LICENSE': await readFile(join(root, 'LICENSE')),
    'bamdude-contract.json': json({ package_version: version, min_bamdude: version, diagnostic_contract: 1, skill_contract: 1, modules: ['base'] }),
    'skills/bamdude-diagnostics/SKILL.md': skill,
    'skills/bamdude-diagnostics/references/diagnostics.md': guide,
  };
  const runtime = { 'server/index.cjs': bundled.outputFiles[0].contents, 'THIRD_PARTY_NOTICES.json': await notices(bundled.metafile.inputs) };
  for (const name of ['src/index.mjs', 'src/connection.mjs', 'package.json', 'package-lock.json']) runtime['source/integrations/mcp-adapter/' + name] = await readFile(join(adapter, name));
  runtime['source/integrations/ai-plugins/build.mjs'] = await readFile(fileURLToPath(import.meta.url));
  runtime['source/integrations/ai-plugins/README.package.md'] = common['README.md'];
  runtime['source/backend/app/core/config.py'] = Buffer.from(`APP_VERSION = "${version}"\n`);
  runtime['source/LICENSE'] = common['LICENSE'];
  runtime['source/integrations/agent-skills/bamdude-diagnostics/SKILL.md'] = skill;
  runtime['source/integrations/agent-skills/bamdude-diagnostics/references/diagnostics.md'] = guide;
  for (const name of ['claude-desktop', 'claude-code', 'portable-plugin']) runtime[`source/integrations/ai-plugins/templates/${name}.json`] = await readFile(join(here, `templates/${name}.json`));
  const template = async name => JSON.parse((await readFile(join(here, `templates/${name}.json`), 'utf8')).replaceAll('@VERSION@', version));
  const desktop = await template('claude-desktop');
  const schema = MANIFEST_SCHEMAS['0.3'];
  if (!schema) throw new Error('MCPB schema unavailable');
  schema.parse(desktop);
  const code = await template('claude-code');
  const portable = await template('portable-plugin');
  const entries = {
    'bamdude-claude-desktop.mcpb': { ...common, ...runtime, 'manifest.json': json(desktop) },
    'bamdude-claude-code.zip': { ...common, ...runtime, '.claude-plugin/plugin.json': json(code) },
    'bamdude-portable-plugin.zip': { ...common, 'plugin.json': json(portable) },
  };
  const digests = {};
  await mkdir(output, { recursive: true });
  for (const [filename, files] of Object.entries(entries)) {
    // Names are generated here, never taken from archive/user input.
    const archive = {};
    for (const [name, bytes] of Object.entries(files).sort(([a], [b]) => a.localeCompare(b))) {
      if (name.startsWith('/') || name.includes('..') || name.includes('\\')) throw new Error('Invalid archive path');
      archive[name] = [bytes, { mtime: new Date('1980-01-01T00:00:00Z') }];
    }
    const bytes = zipSync(archive, { level: 9 });
    await writeFile(join(output, filename), bytes);
    digests[filename] = hash(bytes);
  }
  const catalog = { package_version: version, diagnostic_contract: 1, skill_contract: 1,
    guide_sha256: hash(guide), skill_sha256: hash(skill), sha256: digests };
  await writeFile(join(output, 'packages.json'), json(catalog));
  await writeFile(join(output, 'SHA256SUMS'), Object.entries(digests).map(([file, digest]) => `${digest}  ${file}`).join('\n') + '\n');
  return catalog;
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  const index = process.argv.indexOf('--out');
  const output = resolve(index >= 0 ? process.argv[index + 1] : join(here, 'dist'));
  await buildPackages(output);
  console.log(`Built BamDude AI packages in ${relative(root, output)}`);
}
