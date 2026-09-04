import { describe, expect, it } from 'vitest';
import fs from 'node:fs';
import path from 'node:path';

/**
 * `['project', id]` and `['product', id]` are DECLARED in exactly one place each.
 *
 * ⚠️ **A query has a single set of options, whoever asked last.** TanStack keeps
 * one entry per query key, and the LAST observer to mount owns its options —
 * including `meta`. So a second `useQuery` on a key the order page or the
 * product page already watches does not "also watch" it: it takes it over. That
 * is not theory. The order page declared `meta: { refreshToast: true }` and its
 * queue panel declared nothing, and the panel silently wiped the flag; a failed
 * background refetch then went unreported on exactly the page the flag was
 * added for. The product page had the same hole under its card dialog.
 *
 * A test rather than a comment because the failure is invisible: nothing warns,
 * nothing throws, and the page keeps rendering the right numbers — it just
 * stops saying when they have gone stale. So the rule is enforced by grep: the
 * key literal may appear only in the hook that owns it, and in the invalidation
 * helper whose whole job is to name keys.
 *
 * ⚠️ Only `queryKey:` DECLARATIONS are matched. `invalidateQueries({ queryKey:
 * […] })` and `removeQueries` name the same keys and are not observers, so they
 * are free to appear anywhere — this test would be unreadable noise if it tried
 * to police those, and they cannot cause the bug.
 */
const SOURCE_ROOT = path.resolve(__dirname, '../../');

/** The hook that OWNS each detail key, relative to `src/`. */
const OWNER: Record<string, string> = {
  project: 'hooks/useOrderDetail.ts',
  product: 'hooks/useProductDetail.ts',
};

/** The invalidation helper is allowed to spell any key out; it observes none.
 *  It declares none today (it builds its keys from `ORDER_VIEW_KEYS` and
 *  `DETAIL_KEY`), so this is a permission rather than an expectation. */
const ALSO_ALLOWED = 'utils/queryInvalidation.ts';

function sourceFiles(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) return entry.name === '__tests__' ? [] : sourceFiles(full);
    return /\.tsx?$/.test(entry.name) ? [full] : [];
  });
}

/** `invalidateQueries`, `removeQueries` and friends name a key without watching
 *  it, and are therefore allowed everywhere — they cannot take a query's options
 *  over because they create no observer. Recognised by the call around them. */
const NOT_AN_OBSERVER = /(invalidate|remove|cancel|refetch|reset)Queries|(get|set)Quer(y|ies)Data|getQueryCache/;

function declarationsOf(key: string): string[] {
  // `queryKey: ['project',` — the declaration form. The bare prefix
  // `['project']` an invalidation uses is deliberately not matched.
  const pattern = new RegExp(String.raw`queryKey:\s*\[\s*'${key}'\s*,`);
  const declares = (text: string) =>
    text.split(/\r?\n/).some((line) => pattern.test(line) && !NOT_AN_OBSERVER.test(line));
  return sourceFiles(SOURCE_ROOT)
    .filter((file) => declares(fs.readFileSync(file, 'utf8')))
    .map((file) => path.relative(SOURCE_ROOT, file).split(path.sep).join('/'));
}

describe('detail query keys have one owner each', () => {
  it.each(Object.entries(OWNER))('["%s", id] is declared only by its hook', (key, owner) => {
    const declaring = declarationsOf(key);
    expect(declaring).toContain(owner);
    expect(declaring.filter((file) => file !== owner && file !== ALSO_ALLOWED)).toEqual([]);
  });

  it('the source walk actually reads files — an empty sweep would pass everything', () => {
    expect(sourceFiles(SOURCE_ROOT).length).toBeGreaterThan(100);
  });
});
