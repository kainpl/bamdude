import { describe, it, expect } from 'vitest';
import en from '../../i18n/locales/en';
import uk from '../../i18n/locales/uk';

/**
 * Recursively extracts all keys from a nested object as dot-notation paths.
 * Example: { foo: { bar: 'baz' } } => ['foo.bar']
 */
const getKeys = (obj: object, prefix = ''): string[] => {
  return Object.entries(obj).flatMap(([key, value]) => {
    const path = prefix ? `${prefix}.${key}` : key;
    return typeof value === 'object' && value !== null
      ? getKeys(value, path)
      : [path];
  });
};

/**
 * Recursively walks the locale tree and yields [path, stringValue] for every
 * leaf whose value is a string. Non-string leaves (numbers, bool, arrays,
 * undefined from spread mistakes) are ignored by `typeof === 'string'` so the
 * non-string-leaves test below catches them explicitly.
 */
const getStringLeaves = (obj: object, prefix = ''): [string, string][] => {
  return Object.entries(obj).flatMap(([key, value]): [string, string][] => {
    const path = prefix ? `${prefix}.${key}` : key;
    if (typeof value === 'string') return [[path, value]];
    if (typeof value === 'object' && value !== null) return getStringLeaves(value, path);
    return [];
  });
};

/** All placeholders ({{name}} / {{ name }}) in an ICU-ish template string. */
const PLACEHOLDER_RE = /\{\{\s*([^{}]+?)\s*\}\}/g;
const placeholders = (s: string): Set<string> => {
  const found = new Set<string>();
  for (const m of s.matchAll(PLACEHOLDER_RE)) found.add(m[1]);
  return found;
};

const setsEqual = (a: Set<string>, b: Set<string>) => {
  if (a.size !== b.size) return false;
  for (const x of a) if (!b.has(x)) return false;
  return true;
};

// CLDR plural categories differ per language — EN has {one, other}, UK has {one, few, many, other}.
// Parity check must normalize these suffixes so `inQueue_other` (en) and `inQueue_many` (uk) count
// as the same logical key. Strip any trailing _one/_two/_few/_many/_other/_zero.
const PLURAL_SUFFIX = /_(one|two|few|many|other|zero)$/;
const normalizeKey = (k: string) => k.replace(PLURAL_SUFFIX, '');

/** Group string leaves by their logical (plural-normalised) key, unioning
 * placeholder sets across all CLDR plural variants. Different plural forms
 * within the same language legitimately share placeholder sets, so we only
 * care that the total set of placeholders for a logical key matches across
 * locales. */
const collectPlaceholdersByLogicalKey = (obj: object): Map<string, Set<string>> => {
  const byKey = new Map<string, Set<string>>();
  for (const [path, value] of getStringLeaves(obj)) {
    const key = normalizeKey(path);
    const set = byKey.get(key) ?? new Set<string>();
    for (const p of placeholders(value)) set.add(p);
    byKey.set(key, set);
  }
  return byKey;
};

describe('i18n locale parity', () => {
  const enKeys = new Set(getKeys(en).map(normalizeKey));
  const ukKeys = new Set(getKeys(uk).map(normalizeKey));

  it('Ukrainian locale has all English logical keys', () => {
    const missingInUkrainian = [...enKeys].filter((k) => !ukKeys.has(k)).sort();
    expect(missingInUkrainian, `Missing ${missingInUkrainian.length} key(s) in Ukrainian locale`).toEqual([]);
  });

  it('English locale has all Ukrainian logical keys', () => {
    const missingInEnglish = [...ukKeys].filter((k) => !enKeys.has(k)).sort();
    expect(missingInEnglish, `Missing ${missingInEnglish.length} key(s) in English locale`).toEqual([]);
  });

  it('both locales cover the same set of logical keys', () => {
    expect(enKeys.size).toBe(ukKeys.size);
  });

  // Regression guards adapted from upstream §11I (check-i18n-parity.mjs) on
  // the minimal side: we stay vitest-native (no extra CLI-gate for 2 locales)
  // but catch the failures that actually bit us before — non-string leaves
  // from spread/method mistakes, and placeholder-set mismatches where one
  // locale drifts to {{n}} while the other uses {{count}} and i18next
  // silently interpolates nothing.

  it('all leaf values are strings (no spread/method/array mistakes)', () => {
    const nonString: { locale: string; path: string; kind: string }[] = [];
    const walk = (locale: string, obj: object, prefix = '') => {
      for (const [key, value] of Object.entries(obj)) {
        const path = prefix ? `${prefix}.${key}` : key;
        if (typeof value === 'string') continue;
        if (typeof value === 'object' && value !== null && !Array.isArray(value)) {
          walk(locale, value, path);
          continue;
        }
        nonString.push({ locale, path, kind: Array.isArray(value) ? 'array' : typeof value });
      }
    };
    walk('en', en);
    walk('uk', uk);
    expect(nonString, `Non-string leaf values found: ${JSON.stringify(nonString, null, 2)}`).toEqual([]);
  });

  it('placeholders match per logical key across locales', () => {
    const enPh = collectPlaceholdersByLogicalKey(en);
    const ukPh = collectPlaceholdersByLogicalKey(uk);

    const mismatches: { key: string; en: string[]; uk: string[] }[] = [];
    // Iterate the intersection — keys present in both locales. (Missing-key
    // parity is covered by the earlier two tests.)
    for (const key of enPh.keys()) {
      if (!ukPh.has(key)) continue;
      const e = enPh.get(key)!;
      const u = ukPh.get(key)!;
      if (!setsEqual(e, u)) {
        mismatches.push({
          key,
          en: [...e].sort(),
          uk: [...u].sort(),
        });
      }
    }

    expect(
      mismatches,
      `Placeholder mismatch in ${mismatches.length} key(s): ${JSON.stringify(mismatches.slice(0, 10), null, 2)}`
    ).toEqual([]);
  });
});

/**
 * The rebalancer's refusal codes are a CLOSED list, defined once in
 * `backend/app/services/queue_rebalance.py::SKIP_REASONS` and translated here
 * key-for-key. A code added on the server with no copy on this side would
 * ship as a raw `autoQueue.rebalance.skipped.<code>` in a toast — the panel and
 * the plan block both translate the reason blind, with no fallback.
 */
describe('the rebalance refusal codes are the backend’s closed list', () => {
  const SKIP_REASONS = [
    'order_resources',
    'not_found',
    'already_assigned',
    'not_filed',
    'pinned',
    'scheduled',
    'staged',
    'located',
    'no_yield',
    // m173: a refused copy of the target file is three answers, not one, because what
    // the operator should do differs — wait, free space, or fix the file.
    'source_unreadable',
    'source_copy_busy',
    'source_spool_full',
    'creation_failed',
    'home_model_idle',
    'no_faster_model',
    'cooldown',
    // rebalance-job-feasibility: a printer is asked about the JOB, and an unread or
    // changed answer has a reason of its own rather than «no faster model».
    'eligibility_unknown',
    'no_eligible_target',
    'eligibility_changed',
  ];

  it('en has exactly those keys', () => {
    expect(Object.keys(en.autoQueue.rebalance.skipped).sort()).toEqual([...SKIP_REASONS].sort());
  });

  it('uk has exactly those keys', () => {
    expect(Object.keys(uk.autoQueue.rebalance.skipped).sort()).toEqual([...SKIP_REASONS].sort());
  });
});


/**
 * The queue-source refusal codes are a CLOSED list too, mirrored from the
 * backend taxonomy (`backend/app/services/queue_sources.py` — one class per
 * code, each with its own HTTP status) into `utils/queueSource.ts` and
 * translated here key-for-key.
 *
 * `queueSourceReasonText` builds the key by INTERPOLATION, so `keysResolve`
 * cannot see it and a missing or misspelt key renders the raw
 * `queueSpool.reason.<code>` at the operator on a failed add. A typo made
 * identically in both files would also pass the parity test above, which is
 * why the list itself is pinned here.
 */
describe('the queue-source refusal codes are the backend’s closed list', () => {
  const QUEUE_SOURCE_REASONS = [
    'source_copy_busy',
    'source_spool_replaced',
    'source_unreadable',
    'source_changed',
    'source_invalid',
    'source_copy_timeout',
    'source_spool_no_space',
    'source_spool_write_failed',
    'source_copy_failed',
  ];

  it('en has exactly those keys', () => {
    expect(Object.keys(en.queueSpool.reason).sort()).toEqual([...QUEUE_SOURCE_REASONS].sort());
  });

  it('uk has exactly those keys', () => {
    expect(Object.keys(uk.queueSpool.reason).sort()).toEqual([...QUEUE_SOURCE_REASONS].sort());
  });

  it('matches the list the frontend actually asks with', async () => {
    const { QUEUE_SOURCE_REASONS: asked } = await import('../../utils/queueSource');
    expect([...asked].sort()).toEqual([...QUEUE_SOURCE_REASONS].sort());
  });
});

/**
 * The order journal's codes are a CLOSED list, defined once in
 * `backend/app/services/order_journal.py::EVENT_KINDS` (spec
 * workshop-order-stage, rule 17) and mirrored in `orderJournal.ts`. The feed
 * builds each sentence's key by interpolation, so `keysResolve` cannot see a
 * missing one — it would render the server's English title instead.
 */
describe('every order journal kind has a sentence', () => {
  const EVENT_KINDS = [
    'order_created', 'status_changed', 'fields_changed', 'responsible_changed', 'stage_changed',
    'line_added', 'line_changed', 'line_removed', 'line_configured',
    'prints_filed', 'prints_unfiled', 'prints_relined', 'print_trashed', 'print_restored', 'defects_recorded',
    'queue_items_filed', 'queue_items_unfiled', 'plan_enqueued', 'plan_submission_cancelled', 'line_rebalanced',
    'surplus_banked', 'procurement_updated', 'parts_recounted',
    'kits_assembled', 'goods_received', 'goods_issued', 'stock_taken', 'goods_written_off', 'goods_stocked',
    'attachment_added', 'attachment_removed', 'cover_changed',
  ];

  it('the frontend asks with the backend’s closed list', async () => {
    const { ORDER_JOURNAL_KINDS } = await import('../../components/projects/orderJournal');
    expect([...ORDER_JOURNAL_KINDS]).toEqual(EVENT_KINDS);
  });

  it.each([
    ['en', en],
    ['uk', uk],
  ])('%s has a sentence for each', (_name, locale) => {
    for (const kind of EVENT_KINDS) expect(locale.orders.timeline.events).toHaveProperty(kind);
  });
});

/**
 * Why an estimate is not whole — two closed lists of codes defined on the server
 * (WS-13 E1 OR5a: `farm_forecast.REASON_ORDER`; ES3: `products._ESTIMATE_REASONS`)
 * and mirrored in `utils/estimateReasons.ts`. A label is looked up by the code, so
 * a missing key would render the code itself (CL5).
 */
describe('every estimate reason has a label', () => {
  const ORDER_REASONS = ['unknown_time', 'unroutable', 'printer_resources', 'material_mismatch', 'needs_slicing', 'no_plate', 'unplanned_parts', 'truncated'];
  const PRODUCT_REASONS = [
    'no_plate',
    'needs_slicing',
    'unknown_time',
    'unknown_weight',
    'unknown_purchase_price',
    'truncated',
    'empty_composition',
  ];

  it('the frontend reads the server’s lists, in their order', async () => {
    const { ORDER_ESTIMATE_REASONS, PRODUCT_ESTIMATE_REASONS } = await import('../../utils/estimateReasons');
    expect([...ORDER_ESTIMATE_REASONS]).toEqual(ORDER_REASONS);
    expect([...PRODUCT_ESTIMATE_REASONS]).toEqual(PRODUCT_REASONS);
  });

  it.each([
    ['en', en],
    ['uk', uk],
  ])('%s has a label for each', (_name, locale) => {
    for (const code of new Set([...ORDER_REASONS, ...PRODUCT_REASONS])) {
      expect(locale.projects.estimateReasons).toHaveProperty(code);
    }
  });
});
