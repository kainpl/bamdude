import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { byLocationName } from '../../utils/locationOrder';

/**
 * Manage the places printers and sensors stand in.
 *
 * Deleting is refused while anything holds a location, and the refusal is
 * explained rather than shown as a raw error: the queued items are the reason,
 * since nulling their target would send that work somewhere nobody chose.
 */
export function PrinterLocationsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [error, setError] = useState<string | null>(null);

  const { data } = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations });
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['printer-locations'] });
    // Printers carry the resolved name, so a rename or a removal has to reach
    // their cache too or the old name lingers until something else refetches.
    queryClient.invalidateQueries({ queryKey: ['printers'] });
  };

  const create = useMutation({
    mutationFn: () => api.createPrinterLocation(name.trim()),
    onSuccess: () => {
      setName('');
      setError(null);
      invalidate();
    },
    onError: () => setError(t('printers.locations.nameTaken')),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deletePrinterLocation(id),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: () => setError(t('printers.locations.inUse')),
  });

  // Sorted here rather than trusted from the server: SQLite orders text by
  // byte, which puts Ґ/Є/І/Ї before А and every lowercase name last.
  const locations = [...(data?.locations ?? [])].sort(byLocationName(loc => loc.name));

  return (
    <div className="bg-bambu-dark-secondary rounded-xl p-4">
      <h3 className="text-white mb-3">{t('printers.locations.title')}</h3>

      {locations.length === 0 && <p className="text-sm text-bambu-gray mb-3">{t('printers.locations.empty')}</p>}

      <ul className="space-y-2 mb-3">
        {locations.map((loc) => (
          <li key={loc.id} className="flex items-center justify-between gap-3">
            <span className="text-white">{loc.name}</span>
            <span className="text-xs text-bambu-gray">
              {t('printers.locations.counts', {
                printers: loc.printer_count,
                sensors: loc.sensor_count,
                queued: loc.queued_count,
              })}
            </span>
            <button type="button" className="text-status-error text-sm" onClick={() => remove.mutate(loc.id)}>
              {t('common.delete')}
            </button>
          </li>
        ))}
      </ul>

      <div className="flex gap-2">
        <input
          className="flex-1 px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder={t('printers.modal.locationPlaceholder')}
        />
        <button
          type="button"
          className="px-3 py-1.5 bg-bambu-green rounded-lg text-white disabled:opacity-50"
          disabled={!name.trim() || create.isPending}
          onClick={() => create.mutate()}
        >
          {t('printers.locations.add')}
        </button>
      </div>

      {error && <p className="text-sm text-status-error mt-2">{error}</p>}
    </div>
  );
}
