import { STOCK_NOTE_TOKENS } from '../../api/client';

/** The seven tokens, as a set, for the one question this file asks of them. */
const NOTE_TOKENS: ReadonlySet<string> = new Set(STOCK_NOTE_TOKENS);

/**
 * Whether a note is one the SERVER wrote, and therefore one to translate.
 *
 * ⚠️ **The token set is closed and the fallback is verbatim, not blank.** The
 * backend writes tokens (Ruling 17) precisely so its half can be read in the
 * operator's language; the other half is a hand correction, whose whole value
 * is the sentence the person typed. Translating by prefix or dropping an
 * unknown note would lose exactly the notes that matter.
 */
export function isNoteToken(note: string): boolean {
  return NOTE_TOKENS.has(note);
}

/** `+5` / `−3`. Signed on purpose: a reversal is a movement too, and a column
 *  of unsigned numbers cannot be read as a ledger. The ledger never writes a
 *  zero, so there is no third case. */
export function signed(delta: number): string {
  return delta > 0 ? `+${delta}` : `−${Math.abs(delta)}`;
}
