/**
 * Deal `totals[i]` copies of plate `i` over `targetCount` printers, one copy
 * at a time, round-robin — the print dialog's «Total» quantity mode (spec
 * 2026-09-11 §3.1). The cursor carries from one plate to the next, so when
 * several plates each leave a remainder the tails are spread over the
 * printers instead of all landing on the first one. Returns
 * `rows[plate][target]`; every row sums to its total, and within a row no two
 * targets differ by more than one.
 */
export function splitRoundRobin(totals: number[], targetCount: number): number[][] {
  let cursor = 0;
  return totals.map((total) => {
    const row = new Array<number>(targetCount).fill(0);
    if (targetCount === 0) return row;
    const copies = Math.max(0, Math.floor(total));
    for (let i = 0; i < copies; i++) {
      row[(cursor + i) % targetCount] += 1;
    }
    cursor = (cursor + copies) % targetCount;
    return row;
  });
}
