import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { PrinterTagChip } from './PrinterTagChip';
import { byLocationName } from '../utils/locationOrder';

interface Props {
  value: number[];
  onChange: (ids: number[]) => void;
  allowCreate?: boolean;
}

/**
 * Chips for the tags a printer carries, and one select to add another.
 *
 * Ids, never names: the same tag typed twice in two forms is what the entity
 * exists to prevent. Inline create mirrors PrinterLocationSelect so both
 * pickers on the printer form behave alike.
 */
export function PrinterTagsSelect({ value, onChange, allowCreate = false }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data } = useQuery({ queryKey: ['printer-tags'], queryFn: api.getPrinterTags });
  const tags = [...(data?.tags ?? [])].sort(byLocationName((tag) => tag.name));
  const chosen = tags.filter((tag) => value.includes(tag.id));
  // Only what is not already worn — offering a chosen tag again would either
  // duplicate an id or do nothing, and both read as a broken picker.
  const available = tags.filter((tag) => !value.includes(tag.id));

  /**
   * ⚠️ The refusal has to be SHOWN.
   *
   * The backend answers a duplicate name with 409 "A tag with this name
   * already exists.", and without an `onError` that lands nowhere: the dialog
   * stays open with the typed name still in it and a Save button that looks
   * live but has already been pressed. The operator's only reading is that the
   * form is broken. The backend's own sentence says which name is taken, so it
   * is what gets shown; the generic line is only the fallback for a refusal
   * that arrived without one.
   */
  const create = useMutation({
    mutationFn: (name: string) => api.createPrinterTag(name),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['printer-tags'] });
      onChange([...value, created.id]);
      setCreating(false);
      setDraft('');
      setError(null);
    },
    onError: (e: Error) => setError(e.message || t('printers.tags.nameTaken')),
  });

  /** Entering or leaving create mode clears a refusal from the last attempt. */
  const setCreateMode = (next: boolean) => {
    setCreating(next);
    setError(null);
  };

  return (
    <div className="space-y-2">
      {chosen.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {chosen.map((tag) => (
            <PrinterTagChip
              key={tag.id}
              tag={tag}
              onRemove={{
                label: t('printers.tags.remove', { name: tag.name }),
                onClick: () => onChange(value.filter((id) => id !== tag.id)),
              }}
            />
          ))}
        </div>
      )}
      {creating ? (
        // ⚠️ The draft is deliberately NOT cleared on a refusal — the name is
        // what has to be edited, and retyping it from scratch is the punishment
        // for a typo the operator can already see.
        <div>
          <div className="flex gap-2">
            <input
              autoFocus
              className="flex-1 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
              value={draft}
              onChange={(e) => setDraft(e.target.value)}
              placeholder={t('printers.tags.add')}
            />
            <button
              type="button"
              className="px-3 py-1.5 bg-bambu-green rounded-lg text-white disabled:opacity-50"
              disabled={!draft.trim() || create.isPending}
              onClick={() => create.mutate(draft.trim())}
            >
              {t('common.save')}
            </button>
            <button type="button" className="px-3 py-1.5 text-bambu-gray" onClick={() => setCreateMode(false)}>
              {t('common.cancel')}
            </button>
          </div>
          {error && <p className="text-xs text-status-error mt-1">{error}</p>}
        </div>
      ) : (
        <div className="flex gap-2">
          {/* ⚠️ `value=""` always: this select is an ADD action, not the state.
              The chips above are what the form holds; leaving the picked option
              selected would show the same tag twice, once as a chip and once as
              the picker's current value. */}
          <select
            aria-label={t('printers.tags.pick')}
            className="flex-1 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
            value=""
            onChange={(e) => {
              if (e.target.value) onChange([...value, Number(e.target.value)]);
            }}
          >
            <option value="">{t('printers.tags.pick')}</option>
            {available.map((tag) => (
              <option key={tag.id} value={tag.id}>
                {tag.name}
              </option>
            ))}
          </select>
          {allowCreate && (
            <button
              type="button"
              className="px-3 py-1.5 text-bambu-green whitespace-nowrap"
              onClick={() => setCreateMode(true)}
            >
              {t('printers.tags.addShort')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
