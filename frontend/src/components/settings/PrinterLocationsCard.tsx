import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../../api/client';
import { byLocationName } from '../../utils/locationOrder';

/**
 * Manage the places printers and sensors stand in.
 *
 * Deleting is refused while anything holds a location — printers, sensors,
 * queued work, or other locations — and the refusal is explained rather than
 * shown as a raw error: the queued items are the reason, since nulling their
 * target would send that work somewhere nobody chose.
 *
 * Moving a location to another parent is here for the same reason the cycle
 * check exists on the backend: without it a mistaken parent could only be
 * fixed by deleting, and deleting is refused while anything stands in the
 * place.
 */
export function PrinterLocationsCard() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [name, setName] = useState('');
  const [parentId, setParentId] = useState<number | null>(null);
  const [error, setError] = useState<string | null>(null);

  const { data } = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations });
  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['printer-locations'] });
    // Printers carry the resolved name, so a rename or a removal has to reach
    // their cache too or the old name lingers until something else refetches.
    queryClient.invalidateQueries({ queryKey: ['printers'] });
  };

  const create = useMutation({
    mutationFn: () => api.createPrinterLocation(name.trim(), parentId),
    onSuccess: () => {
      setName('');
      setError(null);
      invalidate();
    },
    // The backend's own sentence: a cycle and a fourth level each say what is
    // wrong, and "name taken" would be a guess that is usually incorrect.
    onError: (e: Error) => setError(e.message || t('printers.locations.nameTaken')),
  });

  const move = useMutation({
    mutationFn: ({ id, parent_id }: { id: number; parent_id: number | null }) =>
      api.updatePrinterLocation(id, { parent_id }),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: Error) => setError(e.message),
  });

  const remove = useMutation({
    mutationFn: (id: number) => api.deletePrinterLocation(id),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: () => setError(t('printers.locations.inUse')),
  });

  // By PATH, so a parent leads its own children. Sorted here rather than
  // trusted from the server: SQLite orders text by byte, which puts Ґ/Є/І/Ї
  // before А and every lowercase name last.
  const locations = [...(data?.locations ?? [])].sort(byLocationName((loc) => loc.path));

  return (
    <div className="bg-bambu-dark-secondary rounded-xl p-4">
      <h3 className="text-white mb-3">{t('printers.locations.title')}</h3>

      {locations.length === 0 && <p className="text-sm text-bambu-gray mb-3">{t('printers.locations.empty')}</p>}

      <ul className="space-y-2 mb-3">
        {locations.map((loc) => (
          <li key={loc.id} className="flex items-center justify-between gap-3">
            {/* Indented by depth: the tree is the point, and a flat list of
                paths repeats every parent on every row. */}
            <span className="text-white" style={{ paddingLeft: (loc.depth - 1) * 16 }} title={loc.path}>
              {loc.name}
            </span>

            <label className="sr-only" htmlFor={`parent-of-${loc.id}`}>
              {t('printers.locations.parent')} {loc.name}
            </label>
            <select
              id={`parent-of-${loc.id}`}
              className="text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded px-1.5 py-1 text-bambu-gray"
              value={loc.parent_id ?? ''}
              onChange={(e) => move.mutate({ id: loc.id, parent_id: e.target.value ? Number(e.target.value) : null })}
            >
              <option value="">{t('printers.locations.noParent')}</option>
              {locations
                // Itself excluded; the backend refuses the deeper cases and its
                // sentence is what gets shown.
                .filter((candidate) => candidate.id !== loc.id)
                .map((candidate) => (
                  <option key={candidate.id} value={candidate.id} title={candidate.path}>
                    {' '.repeat((candidate.depth - 1) * 3)}
                    {candidate.name}
                  </option>
                ))}
            </select>

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
        <label className="sr-only" htmlFor="new-location-parent">
          {t('printers.locations.parent')}
        </label>
        <select
          id="new-location-parent"
          className="px-2 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
          value={parentId ?? ''}
          onChange={(e) => setParentId(e.target.value ? Number(e.target.value) : null)}
        >
          <option value="">{t('printers.locations.noParent')}</option>
          {locations.map((loc) => (
            <option key={loc.id} value={loc.id} title={loc.path}>
              {' '.repeat((loc.depth - 1) * 3)}
              {loc.name}
            </option>
          ))}
        </select>
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
