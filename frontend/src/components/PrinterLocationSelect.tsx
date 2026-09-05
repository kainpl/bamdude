import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import { byLocationName } from '../utils/locationOrder';

interface Props {
  value: number | null;
  onChange: (id: number | null) => void;
  allowCreate?: boolean;
}

/**
 * One select, shared by the printer form and the auto-queue dialog.
 *
 * Those were two independent free-text inputs, so the same place had to be
 * typed twice and matched exactly — a slip in either meant queued work waited
 * for a location no printer was in, silently. Sharing the component is what
 * makes that impossible rather than merely unlikely.
 */
export function PrinterLocationSelect({ value, onChange, allowCreate = false }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data } = useQuery({
    queryKey: ['printer-locations'],
    queryFn: api.getPrinterLocations,
  });

  /**
   * ⚠️ The refusal has to be SHOWN.
   *
   * A duplicate name is refused with 409 "A location with this name already
   * exists." — and without an `onError` that lands nowhere: the row stays open
   * with the typed name still in it and a Save button that looks live but has
   * already been pressed, so the only available reading is that the form is
   * broken. The backend's own sentence is what gets shown (a cycle and a
   * fourth level each say something different); the generic line is only the
   * fallback for a refusal that arrived without one.
   */
  const create = useMutation({
    mutationFn: (name: string) => api.createPrinterLocation(name),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['printer-locations'] });
      onChange(created.id);
      setCreating(false);
      setDraft('');
      setError(null);
    },
    onError: (e: Error) => setError(e.message || t('printers.locations.nameTaken')),
  });

  /** Entering or leaving create mode clears a refusal from the last attempt. */
  const setCreateMode = (next: boolean) => {
    setCreating(next);
    setError(null);
  };

  if (creating) {
    return (
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
            placeholder={t('printers.modal.locationPlaceholder')}
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
    );
  }

  return (
    <div className="flex gap-2">
      <select
        className="flex-1 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value ? Number(e.target.value) : null)}
      >
        {/* A printer without a place is a normal state, so it needs a real
            choice rather than an empty first row that reads as unset-by-accident. */}
        <option value="">{t('printers.ungrouped')}</option>
        {/* Sorted by PATH so a parent leads its own children, and labelled by
            NAME because the indent already says whose child it is — a full path
            on every row turns a three-level list into mush. The path stays in
            the title. */}
        {[...(data?.locations ?? [])]
          .sort(byLocationName((loc) => loc.path))
          .map((loc) => (
            <option key={loc.id} value={loc.id} title={loc.path}>
              {' '.repeat((loc.depth - 1) * 3)}
              {loc.name}
            </option>
          ))}
      </select>
      {allowCreate && (
        <button type="button" className="px-3 py-1.5 text-bambu-green whitespace-nowrap" onClick={() => setCreateMode(true)}>
          {t('printers.locations.addShort')}
        </button>
      )}
    </div>
  );
}
