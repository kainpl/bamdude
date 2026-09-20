/** 1 = S, 2 = M, 3 = L, 4 = XL — the same scale on every page that has one. */
export const CARD_SIZE_LABELS = ['S', 'M', 'L', 'XL'] as const;

/**
 * Read a persisted card size. Anything that is not 1–4 — a missing key, an
 * older value, a hand-edited one — falls back to M, so a page never mounts
 * with a size its grid table has no row for.
 */
export function readStoredCardSize(key: string, fallback = 2): number {
  const saved = parseInt(localStorage.getItem(key) ?? '', 10);
  return saved >= 1 && saved <= 4 ? saved : fallback;
}
