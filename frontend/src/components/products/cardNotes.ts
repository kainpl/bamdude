import type { TFunction } from 'i18next';
import type { CardNote } from '../../api/client';
import { formatFileSize } from '../../utils/file';

/**
 * One `CardNote` in the operator's language.
 *
 * ⚠️ **The server never sends prose.** A fill, a re-read and a ZIP import all
 * answer with CODES plus params, because the backend has no idea which language
 * the person reading them speaks. This is the only place that knows, so every
 * surface that shows a note — the product header's re-read toast, the import
 * dialog's warnings, the model card — must come through here rather than grow
 * its own copy of the switch; two copies is how one of them ends up missing the
 * codes the other has.
 *
 * `field` and `category` are raw server vocabulary (`design_id`, `bom_docs`)
 * and are translated before they reach the sentence; `size` and `limit` are
 * bytes and are formatted. `category` may also be the literal `"cover"`, which
 * only the import produces.
 *
 * ⚠️ **An unknown code renders as the code, not as a key path.** The wire's set
 * of codes is closed today and can grow in a release the frontend was not
 * rebuilt for; `products.card.notes.import_plate_missing` on screen tells an
 * operator nothing, while `import_plate_missing` is at least a thing to search
 * for and a thing to paste into a bug report.
 */
export function cardNoteText(t: TFunction, note: CardNote): string {
  const params: Record<string, string | number> = { ...note.params };
  if (typeof params.field === 'string') params.field = t(`products.card.fields.${params.field}`);
  if (typeof params.category === 'string') {
    params.category = t(`products.attachments.category.${params.category}`);
  }
  if (typeof params.size === 'number') params.size = formatFileSize(params.size);
  if (typeof params.limit === 'number') params.limit = formatFileSize(params.limit);
  return t(`products.card.notes.${note.code}`, { ...params, defaultValue: note.code });
}

/** Every note as one line — they are one answer to one question, and a toast
 *  per note would push the first off screen before it is read. */
export function cardNotesText(t: TFunction, notes: CardNote[]): string {
  return notes.map((note) => cardNoteText(t, note)).join(' · ');
}
