/**
 * Every dropdown is `components/Select.tsx` (vault: inv-one-select-primitive).
 *
 * Before the primitive, "pick one from a list" was written from scratch at each
 * call site: an inventory on 1bc637e7 found 207 native `<select>`s carrying 75
 * different sets of classes, and a shared class string copied into a dozen
 * files in seven different values. The migration (v0.6.0) moved 191 of them to
 * the one control; this scan is what keeps the number from growing back.
 *
 * The five that stayed native are listed below BY FILE AND COUNT, each with
 * the reason the primitive does not describe it. The list is checked both
 * ways: a native `<select>` outside it (or one more inside a listed file) is a
 * new offender, and a listed file that no longer has its count has been
 * migrated and must leave the list — an allowlist nobody prunes is a second
 * copy of the problem. To add an exception, argue it in the vault note first.
 */
import { describe, it, expect } from 'vitest';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, sep } from 'node:path';

const SRC = join(process.cwd(), 'src');
const PRIMITIVE = 'components/Select.tsx';

/** Native `<select>` tags the primitive deliberately does not cover. */
const ALLOWLIST: Record<string, { count: number; why: string }> = {
  'components/PrintModal/PrinterSelector.tsx': {
    count: 1,
    why: 'slot picker whose border is coloured by validation state (filament match) — a third state family beside `tone` and `active`',
  },
  'components/PrintModal/FilamentMapping.tsx': {
    count: 1,
    why: 'same validation-state border as PrinterSelector',
  },
  'components/SlicerSettingsPanel.tsx': {
    count: 2,
    why: 'compact settings grid on its own py-0.5/text-xs scale, shared with its inputs',
  },
  'pages/PrintersPage.tsx': {
    count: 1,
    why: 'drying popover on the printer card: amber accent and the --pc-* type scale of the popover',
  },
};

function walk(dir: string, out: string[] = []): string[] {
  for (const d of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, d.name);
    if (d.isDirectory()) {
      if (d.name !== '__tests__') walk(p, out);
    } else if (d.name.endsWith('.tsx')) {
      out.push(p);
    }
  }
  return out;
}

/** Lines that open a native `<select` tag; prose in comments does not count. */
function nativeSelectLines(src: string): number[] {
  const out: number[] = [];
  src.split('\n').forEach((line, i) => {
    const trimmed = line.trimStart();
    if (/^(\/\/|\*|\/\*|\{\/\*)/.test(trimmed)) return;
    if (/(^|[^\w/*])<select\b/.test(line)) out.push(i + 1);
  });
  return out;
}

describe('one Select primitive', () => {
  const found = new Map<string, number[]>();
  for (const file of walk(SRC)) {
    const rel = relative(SRC, file).split(sep).join('/');
    if (rel === PRIMITIVE) continue;
    const lines = nativeSelectLines(readFileSync(file, 'utf8'));
    if (lines.length) found.set(rel, lines);
  }

  it('renders every native <select> outside the primitive from the allowlist, and nothing more', () => {
    const offenders: string[] = [];
    for (const [rel, lines] of found) {
      const allowed = ALLOWLIST[rel];
      if (!allowed) offenders.push(`${rel}:${lines.join(',')} — use <Select> (components/Select.tsx)`);
      else if (lines.length !== allowed.count)
        offenders.push(`${rel} has ${lines.length} native <select> (lines ${lines.join(',')}), allowlist says ${allowed.count}`);
    }
    expect(offenders).toEqual([]);
  });

  it('keeps no stale allowlist entry — a migrated file leaves the list', () => {
    const stale = Object.keys(ALLOWLIST).filter((rel) => !found.has(rel));
    expect(stale).toEqual([]);
  });
});
