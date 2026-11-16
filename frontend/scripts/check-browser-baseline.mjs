#!/usr/bin/env node
/**
 * Fail the build when the bundle uses a JS feature our oldest supported browser
 * cannot parse (#2971).
 *
 * Why this exists as a grep rather than a build target: Vite's `build.target`
 * only governs *syntax lowering*. esbuild does not rewrite regular expressions,
 * so a lookbehind assertion - unsupported before Safari 16.4 - builds silently
 * under `safari15`, `safari16.0` and `es2020` alike (measured, all three). That
 * is exactly how #2971 shipped: `remark-gfm` pulled a lookbehind regex literal
 * into the entry chunk, iOS 16.0-16.3 refused to compile the module, and every
 * page rendered as a blank white screen from v1.2.5 until it was found in the
 * field two months later.
 *
 * A regex literal is validated when its module is *compiled*, so one of these
 * anywhere in the entry chunk takes down the entire app, not just the feature
 * that pulled it in. There is no graceful degradation to fall back on, which is
 * why this is a hard build failure and not a warning.
 *
 * BASELINE: Safari 16.0 / iOS 16.0. Raising it is a product decision - if you
 * do, drop the entries that the new floor supports rather than deleting the
 * check.
 *
 * Scope: parse-time failures only. Runtime APIs (`Object.groupBy`,
 * `Promise.withResolvers`, ...) break one feature rather than the whole bundle
 * and are better caught by real-browser testing, so they are deliberately not
 * listed here.
 */

import { readdirSync, readFileSync } from 'node:fs';
import { join, dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// The build checks the tracked bundle; a test hands in a fixture directory instead.
const ASSETS = process.argv[2]
  ? resolve(process.argv[2])
  : resolve(dirname(fileURLToPath(import.meta.url)), '..', '..', 'static', 'assets');

/**
 * Each pattern must match only real occurrences of the feature. Anything that
 * needs context to tell a false positive from a real hit (regex flags, for
 * instance, are indistinguishable from division by a variable in a minified
 * bundle without parsing) is left out rather than made noisy.
 */
const FORBIDDEN = [
  {
    pattern: /\(\?<[=!]/g,
    feature: 'regex lookbehind assertion',
    since: 'Safari 16.4',
    hint: 'A dependency shipped `(?<=` or `(?<!` in a regex literal. Find it with:\n'
      + '      grep -rl \'(?<[=!]\' --include=*.js node_modules/\n'
      + '    then avoid importing that module (see src/utils/remarkGfmNoAutolink.ts).',
  },
  {
    // The one pattern here that can in principle fire on a string literal
    // containing the text `static {`. No bundle has ever hit it, and the
    // snippet printed above makes such a hit obvious at a glance - if that is
    // what you are looking at, narrow this pattern rather than deleting it.
    pattern: /\bstatic\s*\{/g,
    feature: 'class static initialisation block',
    since: 'Safari 16.4',
    hint: 'Set `build.target` low enough that esbuild lowers it, or drop the dependency.',
  },
];

/**
 * Every script the bundle can emit, wherever Vite put it: a worker a dependency
 * ships as `.mjs`, a chunk in a sub-directory. The browser compiles each of them,
 * so each is read - a top-level `.js` listing let those through unchecked.
 */
const SCRIPT = /\.(js|mjs|cjs)$/;

function collectScripts(dir, prefix = '') {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const name = prefix + entry.name;
    if (entry.isDirectory()) return collectScripts(join(dir, entry.name), `${name}/`);
    return entry.isFile() && SCRIPT.test(entry.name) ? [name] : [];
  });
}

let bundles;
try {
  bundles = collectScripts(ASSETS).sort();
} catch {
  console.error(`check-browser-baseline: no build output at ${ASSETS} - run \`vite build\` first.`);
  process.exit(1);
}

if (bundles.length === 0) {
  console.error(`check-browser-baseline: no .js / .mjs / .cjs files in ${ASSETS} - did the build succeed?`);
  process.exit(1);
}

const failures = [];

for (const name of bundles) {
  const source = readFileSync(join(ASSETS, name), 'utf8');
  for (const { pattern, feature, since, hint } of FORBIDDEN) {
    const hits = source.match(pattern);
    if (!hits) continue;
    const index = source.search(pattern);
    failures.push(
      `  ${name}: ${hits.length}x ${feature} (requires ${since})\n`
      + `    ...${source.slice(Math.max(0, index - 70), index + 70).replace(/\n/g, ' ')}...\n`
      + `    ${hint}`,
    );
  }
}

if (failures.length > 0) {
  console.error(
    `\ncheck-browser-baseline: bundle uses syntax that Safari 16.0 / iOS 16.0 cannot parse.\n`
    + `A parse error takes down the WHOLE app on those browsers - blank white screen (#2971).\n\n`
    + `${failures.join('\n\n')}\n`,
  );
  process.exit(1);
}

console.log(`✓ ${bundles.length} bundle(s) parse-compatible with the Safari 16.0 baseline.`);
