import { useState } from 'react';
import { X } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
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

  const { data } = useQuery({ queryKey: ['printer-tags'], queryFn: api.getPrinterTags });
  const tags = [...(data?.tags ?? [])].sort(byLocationName((tag) => tag.name));
  const chosen = tags.filter((tag) => value.includes(tag.id));
  // Only what is not already worn — offering a chosen tag again would either
  // duplicate an id or do nothing, and both read as a broken picker.
  const available = tags.filter((tag) => !value.includes(tag.id));

  const create = useMutation({
    mutationFn: (name: string) => api.createPrinterTag(name),
    onSuccess: (created) => {
      queryClient.invalidateQueries({ queryKey: ['printer-tags'] });
      onChange([...value, created.id]);
      setCreating(false);
      setDraft('');
    },
  });

  return (
    <div className="space-y-2">
      {chosen.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {chosen.map((tag) => (
            <span
              key={tag.id}
              className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-bambu-dark-tertiary text-white text-xs"
            >
              {tag.name}
              <button
                type="button"
                className="text-bambu-gray hover:text-white"
                aria-label={t('printers.tags.remove', { name: tag.name })}
                onClick={() => onChange(value.filter((id) => id !== tag.id))}
              >
                <X className="w-3 h-3" />
              </button>
            </span>
          ))}
        </div>
      )}
      {creating ? (
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
          <button type="button" className="px-3 py-1.5 text-bambu-gray" onClick={() => setCreating(false)}>
            {t('common.cancel')}
          </button>
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
              onClick={() => setCreating(true)}
            >
              {t('printers.tags.addShort')}
            </button>
          )}
        </div>
      )}
    </div>
  );
}
