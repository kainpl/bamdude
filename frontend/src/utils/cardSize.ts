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

/** Remembers a size the user picked. A private window, or storage that is
 *  full or blocked, throws on a write: the size still holds for this page. */
export function storeCardSize(key: string, size: number): void {
  try {
    localStorage.setItem(key, String(size));
  } catch {
    // Not remembered; nothing else depends on it.
  }
}
