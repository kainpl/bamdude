/**
 * The id lists behind the staggered-start group pickers.
 *
 * Lives beside the component rather than inside it because
 * `react-refresh/only-export-components` needs a component file to export only
 * components — and the parser is worth testing on its own, so it has to be
 * importable.
 */

/** The JSON array a setting holds, or nothing — a malformed value is treated as empty, never thrown. */
export function parseIdList(raw: string | undefined | null): number[] {
  try {
    const parsed = JSON.parse(raw || '[]');
    return Array.isArray(parsed) ? parsed.filter((v): v is number => Number.isInteger(v)) : [];
  } catch {
    return [];
  }
}

/**
 * The list with `id` added or removed, back as the JSON string the setting
 * stores. Sorted, because the backend normalises to sorted-unique anyway and an
 * unsorted local copy would read as a change that isn't one.
 */
export function toggleId(list: number[], id: number): string {
  return JSON.stringify(list.includes(id) ? list.filter((v) => v !== id) : [...list, id].sort((a, b) => a - b));
}

/** The JSON object a limits setting holds (`{"5": 2}`), or nothing — malformed is empty, never thrown. */
export function parseLimitMap(raw: string | undefined | null): Record<number, number> {
  try {
    const parsed: unknown = JSON.parse(raw || '{}');
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return {};
    const out: Record<number, number> = {};
    for (const [key, value] of Object.entries(parsed as Record<string, unknown>)) {
      const id = Number(key);
      if (Number.isInteger(id) && typeof value === 'number' && Number.isInteger(value) && value >= 1) out[id] = value;
    }
    return out;
  } catch {
    return {};
  }
}

/**
 * The map with `id` set to `value`, or removed when `value` is null, back as
 * the JSON string the setting stores. Keys ascend numerically, which is the
 * order the backend normalises to as well.
 *
 * ⚠️ The ORDER matches; the SPACING does not. Python's `json.dumps` writes
 * `{"5": 1, "10": 2}` and `JSON.stringify` writes `{"5":1,"10":2}`, so the
 * settings page — which compares the raw strings — can read a value that came
 * back from the server as dirty and re-save the identical map. Harmless, and
 * exactly what the id lists already do; noted so the next reader does not go
 * looking for a lost edit.
 */
export function setLimit(raw: string | undefined | null, id: number, value: number | null): string {
  const map = parseLimitMap(raw);
  if (value === null) delete map[id];
  else map[id] = value;
  const keys = Object.keys(map).map(Number).sort((a, b) => a - b);
  return JSON.stringify(Object.fromEntries(keys.map((k) => [String(k), map[k]])));
}
