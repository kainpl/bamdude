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

  const { data: deviceList } = useQuery({
    queryKey: ['zigbee-devices'],
    queryFn: api.getZigbeeDevices,
    enabled: sensor === null,
  });

  const free = (deviceList?.devices ?? []).filter((d) => d.kind === 'sensor' && !d.adopted);

  const done = () => {
    queryClient.invalidateQueries({ queryKey: ['zigbee-sensors'] });
    // Adoption flips `adopted` in the paired list, so that cache is stale too.
    queryClient.invalidateQueries({ queryKey: ['zigbee-devices'] });
    onClose();
  };

  const save = useMutation({
    mutationFn: () =>
      sensor
        ? api.updateZigbeeSensor(sensor.id, { name: name.trim(), location_id: locationId })
        : api.adoptZigbeeSensor({ zigbee_ieee: ieee, name: name.trim(), location_id: locationId }),
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
          <label className="block text-sm text-bambu-gray mb-1">{t('printers.modal.locationGroup')}</label>
          <PrinterLocationSelect value={locationId} onChange={setLocationId} allowCreate />
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
