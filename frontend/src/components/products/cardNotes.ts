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
 * `field` and `category` are raw server vocabulary (`design_id`, `bom_docs`,
 * plus `cover` and `files`, which only the import produces) and are translated
 * before they reach the sentence; `size` and `limit` are bytes and are
 * formatted.
 *
 * ⚠️ **Nothing here can render a key path — not the code, not a param.** An
 * unknown CODE is at least a closed set that can grow in a release the frontend
 * was not rebuilt for. A `category` PARAM is worse than that: `import_bad_category`
 * is fired precisely when the value is NOT one BamDude has
 * (`category not in ATTACHMENT_CATEGORIES`), so its category is foreign text by
 * construction and translating it can only ever miss. The note that came out
 * read "Skipped foo.exe — “products.attachments.category.exe” is not a category
 * here." Every lookup below therefore falls back to the raw value, which is the
 * thing the operator needs to see anyway.
 */
export function cardNoteText(t: TFunction, note: CardNote): string {
  const params: Record<string, string | number> = { ...note.params };
  if (typeof params.field === 'string') {
    params.field = t(`products.card.fields.${params.field}`, { defaultValue: params.field });
  }
  if (typeof params.category === 'string') {
    params.category = t(`products.attachments.category.${params.category}`, {
      defaultValue: params.category,
    });
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
