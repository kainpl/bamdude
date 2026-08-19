import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api } from '../../api/client';
import type { ZigbeeDevice, ZigbeeSensor } from '../../api/client';
import { PrinterLocationSelect } from '../PrinterLocationSelect';
import { Button } from '../Button';

interface Props {
  /** Set when editing, null when adopting. */
  sensor: ZigbeeSensor | null;
  /** Preselected device when the operator started from the paired list. */
  initialDevice: ZigbeeDevice | null;
  onClose: () => void;
}

/**
 * One dialog for adopting and for editing.
 *
 * The only difference is the device picker, present when adopting and absent
 * when editing: a sensor's device does not change, and moving to another one
 * means unbinding and adopting again.
 */
export function SensorFormModal({ sensor, initialDevice, onClose }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();

  const [ieee, setIeee] = useState<string>(sensor?.ieee ?? initialDevice?.ieee ?? '');
  // The hardware name is a DRAFT: five identical SNZBs carry the same string,
  // so it is a starting point rather than an answer.
  const [name, setName] = useState<string>(sensor?.name ?? initialDevice?.name ?? initialDevice?.model ?? '');
  const [locationId, setLocationId] = useState<number | null>(sensor?.location?.id ?? null);
  const [printerId, setPrinterId] = useState<number | null>(sensor?.printer_id ?? null);
  // Which question this sensor answers. A sensor already bound to a printer
  // opens on that side; everything else opens on the place, which is what the
  // binding has always been and what a room thermometer wants.
  const [boundTo, setBoundTo] = useState<'location' | 'printer'>(
    sensor?.printer_id != null ? 'printer' : 'location',
  );

  const { data: deviceList } = useQuery({
    queryKey: ['zigbee-devices'],
    queryFn: api.getZigbeeDevices,
    enabled: sensor === null,
  });

  const free = (deviceList?.devices ?? []).filter((d) => d.kind === 'sensor' && !d.adopted);

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
  });

  const done = () => {
    queryClient.invalidateQueries({ queryKey: ['zigbee-sensors'] });
    // Adoption flips `adopted` in the paired list, so that cache is stale too.
    queryClient.invalidateQueries({ queryKey: ['zigbee-devices'] });
    onClose();
  };

  // ⚠️ BOTH keys are always sent, and the unchosen one as null. The two are
  // exclusive, so leaving the other out would keep an old binding alive beside
  // the new one — the backend clears it either way, but a payload that says
  // only half of what the dialog shows is how that stops being true.
  const binding = {
    location_id: boundTo === 'location' ? locationId : null,
    printer_id: boundTo === 'printer' ? printerId : null,
  };

  const save = useMutation({
    mutationFn: () =>
      sensor
        ? api.updateZigbeeSensor(sensor.id, { name: name.trim(), ...binding })
        : api.adoptZigbeeSensor({ zigbee_ieee: ieee, name: name.trim(), ...binding }),
    onSuccess: done,
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4">
      <div className="bg-bambu-dark-secondary rounded-xl p-5 w-full max-w-md space-y-4">
        <h3 className="text-white">
          {sensor ? t('settings.zigbee.sensors.editTitle') : t('settings.zigbee.sensors.adoptTitle')}
        </h3>

        {sensor === null && (
          <div>
            <label className="block text-sm text-bambu-gray mb-1" htmlFor="sensor-device">
              {t('settings.zigbee.sensors.device')}
            </label>
            <select
              id="sensor-device"
              className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
              value={ieee}
              onChange={(e) => {
                setIeee(e.target.value);
                const picked = free.find((d) => d.ieee === e.target.value);
                if (picked && !name.trim()) setName(picked.name || picked.model || '');
              }}
            >
              <option value="">{t('settings.zigbee.sensors.pickDevice')}</option>
              {free.map((d) => (
                <option key={d.ieee} value={d.ieee}>
                  {d.name || d.model || d.ieee}
                </option>
              ))}
            </select>
            {free.length === 0 && (
              <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">
                {t('settings.zigbee.sensors.noFreeDevices')}
              </p>
            )}
          </div>
        )}

        <div>
          <label className="block text-sm text-bambu-gray mb-1" htmlFor="sensor-name">
            {t('settings.zigbee.sensors.nameLabel')}
          </label>
          <input
            id="sensor-name"
            className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
        </div>

        <div>
          <label className="block text-sm text-bambu-gray mb-1">{t('settings.zigbee.sensors.boundTo')}</label>
          {/* A choice, not a guess. An enclosure probe belongs to one machine
              and a room thermometer to the room; the hardware is identical, so
              only the operator knows which. Where the reading is drawn follows
              from this and nothing else. */}
          <div className="flex gap-1 mb-2" role="radiogroup" aria-label={t('settings.zigbee.sensors.boundTo')}>
            {(['location', 'printer'] as const).map((option) => (
              <button
                key={option}
                type="button"
                role="radio"
                aria-checked={boundTo === option}
                onClick={() => setBoundTo(option)}
                className={`flex-1 px-3 py-1.5 rounded-lg text-sm transition-colors ${
                  boundTo === option
                    ? 'bg-bambu-green text-white'
                    : 'bg-bambu-dark text-bambu-gray hover:text-white'
                }`}
              >
                {t(`settings.zigbee.sensors.boundTo${option === 'location' ? 'Location' : 'Printer'}`)}
              </button>
            ))}
          </div>

          {boundTo === 'location' ? (
            <PrinterLocationSelect value={locationId} onChange={setLocationId} allowCreate />
          ) : (
            <select
              id="sensor-printer"
              aria-label={t('settings.zigbee.sensors.boundToPrinter')}
              className="w-full px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
              value={printerId ?? ''}
              onChange={(e) => setPrinterId(e.target.value === '' ? null : Number(e.target.value))}
            >
              <option value="">{t('settings.zigbee.sensors.pickPrinter')}</option>
              {(printers ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </select>
          )}
          <p className="text-xs text-bambu-gray mt-1">
            {t(
              boundTo === 'location'
                ? 'settings.zigbee.sensors.boundToLocationHint'
                : 'settings.zigbee.sensors.boundToPrinterHint',
            )}
          </p>
        </div>

        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button
            disabled={!name.trim() || (sensor === null && !ieee) || save.isPending}
            onClick={() => save.mutate()}
          >
            {t('common.save')}
          </Button>
        </div>
      </div>
    </div>
  );
}
