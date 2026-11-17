import { useMemo, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { MapPin } from 'lucide-react';
import { api, type Printer } from '../api/client';
import { useToast } from '../contexts/ToastContext';
import { byLocationName } from '../utils/locationOrder';
import { Button } from './Button';
import { Modal } from './Modal';
import { PrinterLocationSelect } from './PrinterLocationSelect';

type TagChoice = 'add' | 'remove';

/**
 * Place the selected printers: one location and / or tags, in one request.
 *
 * The server applies it to all of them or to none, so there is no per-printer
 * result list to read: either the farm is placed as asked, or the refusal says
 * why and nothing moved. The selection is a snapshot taken at the click.
 */
export function BulkPrinterPlacementDialog({ printers, onClose }: { printers: Printer[]; onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [changeLocation, setChangeLocation] = useState(false);
  const [locationId, setLocationId] = useState<number | null>(null);
  const [tagChoices, setTagChoices] = useState<Record<number, TagChoice>>({});

  const { data: tagData } = useQuery({ queryKey: ['printer-tags'], queryFn: api.getPrinterTags });
  const tags = tagData?.tags ?? [];
  const total = printers.length;

  // Where the selection stands now, so the change is made knowingly.
  const places = useMemo(() => {
    const counts = new Map<string, number>();
    for (const printer of printers) {
      const name = printer.location?.path ?? t('printers.ungrouped');
      counts.set(name, (counts.get(name) ?? 0) + 1);
    }
    return [...counts.entries()].sort(byLocationName(([name]) => name));
  }, [printers, t]);

  const carriers = (tagId: number) => printers.filter((p) => p.tag_ids.includes(tagId)).length;

  const choose = (tagId: number, choice: TagChoice | null) =>
    setTagChoices((current) => {
      const next = { ...current };
      if (choice === null) delete next[tagId];
      else next[tagId] = choice;
      return next;
    });

  const addTagIds = Object.entries(tagChoices).filter(([, c]) => c === 'add').map(([id]) => Number(id));
  const removeTagIds = Object.entries(tagChoices).filter(([, c]) => c === 'remove').map(([id]) => Number(id));
  const nothingChosen = !changeLocation && addTagIds.length === 0 && removeTagIds.length === 0;

  const apply = useMutation({
    mutationFn: () =>
      api.placePrinters({
        printer_ids: printers.map((p) => p.id),
        set_location: changeLocation,
        location_id: changeLocation ? locationId : null,
        add_tag_ids: addTagIds,
        remove_tag_ids: removeTagIds,
      }),
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['printers'] });
      queryClient.invalidateQueries({ queryKey: ['printer-locations'] });
      queryClient.invalidateQueries({ queryKey: ['printer-tags'] });
      showToast(t('printers.bulk.placement.done', { count: result.updated }));
      onClose();
    },
  });

  const choiceButton = (tagId: number, choice: TagChoice | null, label: string, disabled = false) => {
    const active = (tagChoices[tagId] ?? null) === choice;
    return (
      <button
        type="button"
        aria-pressed={active}
        disabled={disabled}
        onClick={() => choose(tagId, choice)}
        className={`px-2 py-1 text-xs rounded transition-colors disabled:opacity-40 ${
          active ? 'bg-bambu-green text-white' : 'bg-bambu-dark text-bambu-gray hover:text-white'
        }`}
      >
        {label}
      </button>
    );
  };

  return (
    <Modal
      onClose={onClose}
      title={t('printers.bulk.placement.title', { count: total })}
      icon={<MapPin className="w-5 h-5 text-bambu-green" />}
      closeDisabled={apply.isPending}
      alert={
        apply.isError ? (
          <p role="alert" className="px-4 py-2 text-sm text-status-error">
            {(apply.error as Error)?.message || t('printers.bulk.failed')}
          </p>
        ) : undefined
      }
      footer={
        <div className="flex justify-end gap-2 p-4">
          <Button variant="secondary" onClick={onClose} disabled={apply.isPending}>
            {t('common.cancel')}
          </Button>
          <Button onClick={() => apply.mutate()} disabled={nothingChosen || apply.isPending}>
            {t('printers.bulk.placement.apply')}
          </Button>
        </div>
      }
    >
      <div className="p-4 space-y-5">
        <section>
          <p className="text-xs text-bambu-gray mb-1">{t('printers.bulk.placement.now')}</p>
          <ul className="flex flex-wrap gap-1.5">
            {places.map(([name, count]) => (
              <li key={name} className="px-2 py-0.5 rounded bg-bambu-dark text-xs text-white">
                {name} · {count}
              </li>
            ))}
          </ul>
        </section>

        <section className="space-y-2">
          <label className="flex items-center gap-2 text-sm text-white">
            <input
              type="checkbox"
              checked={changeLocation}
              onChange={(e) => setChangeLocation(e.target.checked)}
              className="accent-bambu-green"
            />
            {t('printers.bulk.placement.changeLocation')}
          </label>
          {changeLocation && <PrinterLocationSelect value={locationId} onChange={setLocationId} allowCreate />}
        </section>

        <section className="space-y-2">
          <p className="text-sm text-white">{t('printers.bulk.placement.tags')}</p>
          {tags.length === 0 ? (
            <p className="text-xs text-bambu-gray">{t('printers.bulk.placement.noTags')}</p>
          ) : (
            <ul className="space-y-1.5">
              {tags.map((tag) => {
                const held = carriers(tag.id);
                return (
                  <li key={tag.id} className="flex items-center gap-2">
                    <span
                      className="w-2.5 h-2.5 rounded-full shrink-0 bg-bambu-gray"
                      style={tag.color ? { backgroundColor: tag.color } : undefined}
                    />
                    <span className="text-sm text-white truncate">{tag.name}</span>
                    <span className="text-xs text-bambu-gray whitespace-nowrap">
                      {t('printers.bulk.placement.tagCount', { count: held, total })}
                    </span>
                    <span className="ml-auto flex gap-1" role="group" aria-label={tag.name}>
                      {choiceButton(tag.id, null, t('printers.bulk.placement.keep'))}
                      {choiceButton(tag.id, 'add', t('printers.bulk.placement.add'), held === total)}
                      {choiceButton(tag.id, 'remove', t('printers.bulk.placement.remove'), held === 0)}
                    </span>
                  </li>
                );
              })}
            </ul>
          )}
        </section>
      </div>
    </Modal>
  );
}
