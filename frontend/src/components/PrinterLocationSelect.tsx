import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';

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

  const { data } = useQuery({
    queryKey: ['printer-locations'],
    queryFn: api.getPrinterLocations,
  });

  const create = useMutation({
    mutationFn: (name: string) => api.createPrinterLocation(name),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['printer-locations'] });
      onChange(created.id);
      setCreating(false);
      setDraft('');
    },
  });

  if (creating) {
    return (
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
        <button type="button" className="px-3 py-1.5 text-bambu-gray" onClick={() => setCreating(false)}>
          {t('common.cancel')}
        </button>
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
        {(data?.locations ?? []).map((loc) => (
          <option key={loc.id} value={loc.id}>
            {loc.name}
          </option>
        ))}
      </select>
      {allowCreate && (
        <button type="button" className="px-3 py-1.5 text-bambu-green whitespace-nowrap" onClick={() => setCreating(true)}>
          {t('printers.locations.addShort')}
        </button>
      )}
    </div>
  );
}
