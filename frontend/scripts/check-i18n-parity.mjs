// Verifies parity across BamDude locale files (en / uk).
// Both locales are strict — BamDude ships en + uk only, and both must always
// agree. Drift of any kind (missing key, extra key, placeholder mismatch,
// plural-form mismatch) fails CI.
//
// Checks performed:
//   1. Leaf-key sets are identical between en and uk.
//   2. Each leaf's {{placeholder}} set is identical.
//   3. Plural suffixes (CLDR-aware for Ukrainian):
//      - Every en key ending in _one / _other / _plural must exist in uk.
//      - Ukrainian has four CLDR plural categories (one / few / many / other)
//        while English has two (one / other), so uk legitimately carries
//        _few and _many variants that en does not. Those are NOT flagged as
//        "extra keys": we require each _few / _many in uk to have a matching
//        _one counterpart in uk (so they're not orphan forms).
//      - uk must NOT introduce an _one / _other key that en does not have
//        (that would mean uk invented a new pluralisable key).
// Malformed input (missing `export default`, parse errors, non-string leaves,
// unsupported property kinds) fails loudly instead of silently passing.
// Exits 1 with a diagnostic report on any failure, else exits 0.
//
// Adapted from upstream Bambuddy's multi-locale parity checker. The upstream
// script supported a strict/informational tier split for 8 locales in various
// stages of completion; BamDude has exactly two locales and both are always
// strict, so the tier logic is removed.

import fs from 'node:fs';
import path from 'node:path';
import url from 'node:url';

const scriptDir = path.dirname(url.fileURLToPath(import.meta.url));
const frontendDir = path.resolve(scriptDir, '..');
const localesDir = path.join(frontendDir, 'src/i18n/locales');
const tsPath = path.join(frontendDir, 'node_modules/typescript/lib/typescript.js');

const tsModule = await import(url.pathToFileURL(tsPath).href);
const ts = tsModule.default ?? tsModule;

function collectLeaves(node, prefix, leaves) {
  if (!ts.isObjectLiteralExpression(node)) return;
  for (const prop of node.properties) {
    if (!ts.isPropertyAssignment(prop)) {
      console.error(
        `Unsupported property kind ${ts.SyntaxKind[prop.kind]} at "${prefix}" ` +
          `(locale files must use plain \`key: value\` assignments — no spread, shorthand, methods, or accessors).`,
      );
      process.exit(1);
    }
    let name;
    if (ts.isIdentifier(prop.name)) name = prop.name.text;
    else if (ts.isStringLiteral(prop.name) || ts.isNoSubstitutionTemplateLiteral(prop.name)) name = prop.name.text;
    else if (ts.isComputedPropertyName(prop.name)) {
      console.error(`ComputedPropertyName not allowed in locale file at path "${prefix}"`);
      process.exit(1);
    } else {
      console.error(`Unsupported property-name kind ${ts.SyntaxKind[prop.name.kind]} at "${prefix}"`);
      process.exit(1);
    }
    const p = prefix ? `${prefix}.${name}` : name;
    if (ts.isObjectLiteralExpression(prop.initializer)) {
      collectLeaves(prop.initializer, p, leaves);
    } else {
      const value = extractStringValue(prop.initializer, p);
      leaves.set(p, value);
    }
  }
}

function extractStringValue(node, keyPath) {
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) return node.text;
  if (ts.isTemplateExpression(node)) {
    let out = node.head.text;
    for (const span of node.templateSpans) {
      out += '${' + span.expression.getText() + '}';
      out += span.literal.text;
    }
    return out;
  }
  console.error(
    `Non-string leaf at "${keyPath}" (kind=${ts.SyntaxKind[node.kind]}): ${node.getText()}\n` +
      `Locale files must only contain string or template literals as leaf values.`,
  );
  process.exit(1);
}

function loadLocale(filePath) {
  const src = fs.readFileSync(filePath, 'utf8');
  const sf = ts.createSourceFile(filePath, src, ts.ScriptTarget.Latest, true);
  if (sf.parseDiagnostics && sf.parseDiagnostics.length > 0) {
    console.error(`${filePath}: ${sf.parseDiagnostics.length} parse error(s):`);
    for (const d of sf.parseDiagnostics.slice(0, 10)) {
      const msg = typeof d.messageText === 'string' ? d.messageText : d.messageText.messageText;
      const { line, character } = sf.getLineAndCharacterOfPosition(d.start ?? 0);
      console.error(`  ${line + 1}:${character + 1} ${msg}`);
    }
    process.exit(1);
  }
  const leaves = new Map();
  let foundExport = false;
  ts.forEachChild(sf, (n) => {
    if (ts.isExportAssignment(n)) {
      foundExport = true;
      collectLeaves(n.expression, '', leaves);
    }
  });
  if (!foundExport) {
    console.error(`${filePath}: no \`export default\` found — locale files must use \`export default { ... }\`.`);
    process.exit(1);
  }
  if (leaves.size === 0) {
    console.error(`${filePath}: \`export default\` resolved to zero leaves — file is empty or not a nested object.`);
    process.exit(1);
  }
  return leaves;
}

const placeholderRe = /\{\{[^{}]+\}\}/g;

// Pure comparison logic, exported so tests can verify each failure mode
// without going through file IO or the TypeScript parser.
// Input:  locales = { en: Map<leafKey, leafString>, uk: Map<leafKey, leafString> }
// Output: { failed, reports: Array<{ label, items }> }
export function compareLocales(locales) {
  if (!locales.en) throw new Error('compareLocales requires a locales.en entry');
  const reports = [];
  const add = (label, items) => {
    if (items.length) reports.push({ label, items });
  };

  const enKeys = new Set(locales.en.keys());

  // Suffixes that are locale-legitimate additions (Ukrainian CLDR categories
  // English doesn't carry). Keys ending in these suffixes in the other locale
  // are tolerated when their _one counterpart exists in the same locale.
  const LOCALE_PLURAL_EXTRAS = {
    uk: ['_few', '_many'],
  };

  // Check 1: key-set equality (plural-aware)
  for (const [code, map] of Object.entries(locales)) {
    if (code === 'en') continue;
    const keys = new Set(map.keys());
    const missing = [...enKeys].filter((k) => !keys.has(k)).sort();

    // Partition extras: CLDR-legitimate plural additions vs actually-extra keys.
    const extrasAllowed = LOCALE_PLURAL_EXTRAS[code] ?? [];
    const extra = [];
    const orphanPlurals = [];
    for (const k of [...keys].sort()) {
      if (enKeys.has(k)) continue;
      const suffix = extrasAllowed.find((s) => k.endsWith(s));
      if (!suffix) {
        extra.push(k);
        continue;
      }
      // Strip the suffix and check that the _one counterpart exists IN THE
      // SAME LOCALE — an orphan _few without a _one is almost certainly a
      // translation mistake, not a real plural form.
      const base = k.slice(0, -suffix.length);
      if (!map.has(`${base}_one`)) {
        orphanPlurals.push(`${k} (no matching ${base}_one in ${code})`);
      }
    }
    add(`${code}: missing keys vs en`, missing);
    add(`${code}: extra keys vs en`, extra);
    add(`${code}: orphan plural forms (no _one counterpart)`, orphanPlurals);
  }

  // Check 2: placeholder set equality per leaf
  for (const [code, map] of Object.entries(locales)) {
    if (code === 'en') continue;
    const mismatches = [];
    for (const [key, enValue] of locales.en) {
      const otherValue = map.get(key);
      if (otherValue === undefined) continue;
      const enPlaceholders = new Set(enValue.match(placeholderRe) ?? []);
      const otherPlaceholders = new Set(otherValue.match(placeholderRe) ?? []);
      const missingPh = [...enPlaceholders].filter((p) => !otherPlaceholders.has(p));
      const extraPh = [...otherPlaceholders].filter((p) => !enPlaceholders.has(p));
      if (missingPh.length || extraPh.length) {
        mismatches.push(
          `${key}: en=${[...enPlaceholders].join(',') || '∅'} vs ${code}=${[...otherPlaceholders].join(',') || '∅'}`,
        );
      }
    }
    add(`${code}: placeholder mismatch vs en`, mismatches);
  }

  // Check 3: plural suffix presence + reverse _one / _other guard
  for (const [code, map] of Object.entries(locales)) {
    if (code === 'en') continue;
    const pluralIssues = [];
    for (const key of enKeys) {
      if (key.endsWith('_plural') && !map.has(key)) pluralIssues.push(`missing _plural key: ${key}`);
      if (key.endsWith('_one') && !map.has(key)) pluralIssues.push(`missing _one key: ${key}`);
      if (key.endsWith('_other') && !map.has(key)) pluralIssues.push(`missing _other key: ${key}`);
    }
    for (const key of map.keys()) {
      if (key.endsWith('_one') && !enKeys.has(key)) {
        pluralIssues.push(`unexpected _one not present in en: ${key}`);
      }
      if (key.endsWith('_other') && !enKeys.has(key)) {
        pluralIssues.push(`unexpected _other not present in en: ${key}`);
      }
    }
    add(`${code}: plural key mismatch`, pluralIssues);
  }

  return { failed: reports.length > 0, reports };
}

// BamDude ships en + uk only; both are strict. Any other file in locales/
// is a configuration error — fail loudly rather than silently ignoring it,
// so nobody accidentally adds a third locale without updating the policy.
const EXPECTED_LOCALES = ['en', 'uk'];

// Skip file IO / process.exit when imported as a library (e.g. from tests).
const isMainModule = import.meta.url === url.pathToFileURL(process.argv[1] ?? '').href;
if (isMainModule) {
  const discovered = fs
    .readdirSync(localesDir)
    .filter((f) => f.endsWith('.ts'))
    .map((f) => f.slice(0, -3))
    .sort();

  const missing = EXPECTED_LOCALES.filter((c) => !discovered.includes(c));
  const extra = discovered.filter((c) => !EXPECTED_LOCALES.includes(c));
  if (missing.length) {
    console.error(`Expected locale file(s) not found in ${localesDir}: ${missing.join(', ')}`);
    process.exit(1);
  }
  if (extra.length) {
    console.error(
      `Unexpected locale file(s) in ${localesDir}: ${extra.join(', ')}\n` +
        `BamDude ships en + uk only. Update EXPECTED_LOCALES in this script + i18n/index.ts + CLAUDE.md if adding a new locale.`,
    );
    process.exit(1);
  }

  const locales = Object.fromEntries(
    EXPECTED_LOCALES.map((c) => [c, loadLocale(path.join(localesDir, `${c}.ts`))]),
  );

  const MAX_REPORT = 20;
  const { reports } = compareLocales(locales);

  if (reports.length) {
    console.error('\n=== i18n parity failures ===');
    for (const { label, items } of reports) {
      console.error(`\n[${label}] (${items.length})`);
      items.slice(0, MAX_REPORT).forEach((i) => console.error(`  ${i}`));
      if (items.length > MAX_REPORT) console.error(`  ... and ${items.length - MAX_REPORT} more`);
    }
  }

  console.log('\nLocale leaf counts:');
  for (const [code, map] of Object.entries(locales)) {
    const tier = code === 'en' ? 'ref' : 'strict';
    console.log(`  ${code.padEnd(4)} ${String(map.size).padEnd(6)} [${tier}]`);
  }

  if (reports.length > 0) {
    console.error(`\n❌ i18n parity check failed (en vs ${EXPECTED_LOCALES.filter((c) => c !== 'en').join(', ')}).`);
    process.exit(1);
  }
  console.log(`\n✓ Locales in parity (en / ${EXPECTED_LOCALES.filter((c) => c !== 'en').join(' / ')}).`);
}
