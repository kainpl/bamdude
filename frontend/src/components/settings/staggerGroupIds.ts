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
