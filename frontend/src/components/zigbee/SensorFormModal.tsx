import { useState } from 'react';
import { NumberInput } from '../NumberInput';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api } from '../../api/client';
import type { ZigbeeDevice, ZigbeeSensor } from '../../api/client';
import type { ZigbeeSensorBinding } from '../../api/client';
import type { ZigbeeSensorBindingInput } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { Modal } from '../Modal';
import { PrinterLocationSelect } from '../PrinterLocationSelect';
import { Button } from '../Button';
import { Select } from '../Select';
import { SensorThresholdsModal } from './SensorThresholdsModal';
import { invalidateSensorViews } from '../../utils/sensorQueryInvalidation';

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
 * Adoption picks a device and one initial target. Editing keeps that device
 * and manages its explicit target bindings.
 */
export function SensorFormModal({ sensor, initialDevice, onClose }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { hasPermission } = useAuth();

  const [ieee, setIeee] = useState<string>(sensor?.ieee ?? initialDevice?.ieee ?? '');
  // The hardware name is a DRAFT: five identical SNZBs carry the same string,
  // so it is a starting point rather than an answer.
  const [name, setName] = useState<string>(sensor?.name ?? initialDevice?.name ?? initialDevice?.model ?? '');
  const [locationId, setLocationId] = useState<number | null>(sensor?.location?.id ?? null);
  const [printerId, setPrinterId] = useState<number | null>(sensor?.printer_id ?? null);
  const [storageId, setStorageId] = useState<number | null>(null);
  // Adoption offers one initial target; editing manages the full target list.
  const [boundTo, setBoundTo] = useState<'location' | 'printer' | 'storage'>(
    sensor?.printer_id != null ? 'printer' : 'location',
  );
  const [targetType, setTargetType] = useState<'printer' | 'room' | 'storage'>('printer');
  const [targetId, setTargetId] = useState<number | null>(null);
  const [notifyEnabled, setNotifyEnabled] = useState(false);
  const [bindingError, setBindingError] = useState<string | null>(null);
  const [pendingRemoval, setPendingRemoval] = useState<number | null>(null);
  const [thresholdBinding, setThresholdBinding] = useState<ZigbeeSensorBinding | null>(null);
  const [bindingDrafts, setBindingDrafts] = useState<Record<number, Partial<ZigbeeSensorBindingInput>>>({});
  const { data: sensorData } = useQuery({ queryKey: ['zigbee-sensors'], queryFn: api.getZigbeeSensors,
    enabled: sensor !== null });
  const currentSensor = sensorData?.sensors.find((row) => row.id === sensor?.id) ?? sensor;
  const duplicateTarget = targetId != null && (currentSensor?.bindings ?? []).some((item) =>
    (targetType === 'printer' && item.printer_id === targetId)
    || (targetType === 'room' && item.printer_location_id === targetId)
    || (targetType === 'storage' && item.storage_location_id === targetId));

  const { data: deviceList } = useQuery({
    queryKey: ['zigbee-devices'],
    queryFn: api.getZigbeeDevices,
    enabled: sensor === null && hasPermission('smart_plugs:read'),
  });

  const free = (deviceList?.devices ?? []).filter((d) => d.kind === 'sensor' && !d.adopted);

  const { data: printers } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: hasPermission('printers:read'),
  });
  const { data: rooms } = useQuery({ queryKey: ['printer-locations'], queryFn: api.getPrinterLocations,
    enabled: sensor !== null && hasPermission('printers:read') });
  const { data: storage } = useQuery({ queryKey: ['inventory-locations'], queryFn: api.getLocations,
    enabled: hasPermission('inventory:read') });

  const done = () => {
    invalidateSensorViews(queryClient);
    // Adoption flips `adopted` in the paired list, so that cache is stale too.
    queryClient.invalidateQueries({ queryKey: ['zigbee-devices'] });
    onClose();
  };

  const initialTarget = boundTo === 'printer' ? printerId : boundTo === 'storage' ? storageId : locationId;
  const initialBinding: ZigbeeSensorBindingInput | undefined = initialTarget == null ? undefined : {
    printer_id: boundTo === 'printer' ? initialTarget : null,
    printer_location_id: boundTo === 'location' ? initialTarget : null,
    storage_location_id: boundTo === 'storage' ? initialTarget : null,
    display_name: null, visible: true, sort_order: 0, notify_enabled: false,
  };

  const save = useMutation({
    mutationFn: () =>
      sensor
        ? api.updateZigbeeSensor(sensor.id, { name: name.trim() })
        : api.adoptZigbeeSensor({ zigbee_ieee: ieee, name: name.trim(), initial_binding: initialBinding }),
    onSuccess: done,
  });

  const addBinding = useMutation({
    mutationFn: () => api.addZigbeeSensorBinding(sensor!.id, {
      printer_id: targetType === 'printer' ? targetId : null,
      printer_location_id: targetType === 'room' ? targetId : null,
      storage_location_id: targetType === 'storage' ? targetId : null,
      display_name: null, visible: true, sort_order: 0, notify_enabled: notifyEnabled,
    }),
    onSuccess: () => { setTargetId(null); setNotifyEnabled(false); setBindingError(null);
      queryClient.invalidateQueries({ queryKey: ['zigbee-sensors'] }); },
    onError: (error: Error) => setBindingError(error.message),
  });
  const removeBinding = useMutation({
    mutationFn: (bindingId: number) => api.deleteZigbeeSensorBinding(sensor!.id, bindingId),
    onSuccess: () => { setBindingError(null); queryClient.invalidateQueries({ queryKey: ['zigbee-sensors'] }); },
    onError: (error: Error) => setBindingError(error.message),
  });
  const updateBinding = useMutation({
    mutationFn: ({ item, changes }: { item: ZigbeeSensorBinding; changes: Partial<ZigbeeSensorBindingInput> }) =>
      api.updateZigbeeSensorBinding(sensor!.id, item.id, {
        printer_id: item.printer_id, printer_location_id: item.printer_location_id,
        storage_location_id: item.storage_location_id, display_name: item.display_name,
        visible: item.visible, sort_order: item.sort_order, notify_enabled: item.notify_enabled,
        ...changes,
      }),
    onSuccess: (_, variables) => { setBindingError(null);
      setBindingDrafts((current) => { const next = { ...current }; delete next[variables.item.id]; return next; });
      queryClient.invalidateQueries({ queryKey: ['zigbee-sensors'] }); },
    onError: (error: Error) => setBindingError(error.message),
  });

  return (
    <>
    <Modal
      onClose={onClose}
      title={sensor ? t('settings.zigbee.sensors.editTitle') : t('settings.zigbee.sensors.adoptTitle')}
      size="md"
    >
      <div className="p-4 space-y-4">
        {sensor === null && (
          <div>
            <label className="block text-sm text-bambu-gray mb-1" htmlFor="sensor-device">
              {t('settings.zigbee.sensors.device')}
            </label>
            <Select
              size="sm"
              className="w-full"
              id="sensor-device"
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
            </Select>
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

        {sensor !== null && <div className="space-y-3">
          <label className="block text-sm text-bambu-gray">{t('settings.zigbee.sensors.boundTo')}</label>
          {(currentSensor?.bindings ?? []).map((item) => {
            const draft = bindingDrafts[item.id] ?? {};
            const change = (fields: Partial<ZigbeeSensorBindingInput>) =>
              setBindingDrafts((current) => ({ ...current, [item.id]: { ...current[item.id], ...fields } }));
            return <div key={item.id} className="space-y-2 rounded-lg bg-bambu-dark p-3 text-sm text-white">
              <div className="flex justify-between gap-2">
                <span>{item.printer_name || item.location?.path || item.storage_location_name || sensor.name}</span>
                <button type="button" className="text-status-error" disabled={removeBinding.isPending}
                  onClick={() => (currentSensor?.bindings ?? []).length === 1
                    ? setPendingRemoval(item.id) : removeBinding.mutate(item.id)}>
                  {t('settings.zigbee.sensors.removeBinding')}
                </button>
              </div>
              <input className="w-full rounded bg-bambu-dark-secondary p-1" maxLength={100}
                aria-label={t('settings.zigbee.sensors.bindingName')}
                placeholder={t('settings.zigbee.sensors.bindingName')}
                value={draft.display_name ?? item.display_name ?? ''}
                onChange={(e) => change({ display_name: e.target.value || null })} />
              <div className="flex flex-wrap items-center gap-3 text-bambu-gray">
                <label className="flex items-center gap-1">
                  <input type="checkbox" checked={draft.visible ?? item.visible}
                    onChange={(e) => change({ visible: e.target.checked })} />
                  {t('settings.zigbee.sensors.visibleBinding')}
                </label>
                <label className="flex items-center gap-1">
                  <input type="checkbox" checked={draft.notify_enabled ?? item.notify_enabled}
                    onChange={(e) => change({ notify_enabled: e.target.checked })} />
                  {t('settings.zigbee.sensors.notifyBinding')}
                </label>
                <label className="flex items-center gap-1">
                  {t('settings.zigbee.sensors.bindingOrder')}
                  <NumberInput className="w-14 rounded bg-bambu-dark-secondary p-1"
                    value={draft.sort_order ?? item.sort_order}
                    onChange={(v) => change({ sort_order: v })}
                    fallback={0} />
                </label>
              </div>
              <div className="flex gap-3">
                <button type="button" onClick={() => setThresholdBinding(item)}>
                  {t('settings.zigbee.thresholds.title')}
                </button>
                <button type="button" disabled={!bindingDrafts[item.id] || updateBinding.isPending}
                  onClick={() => updateBinding.mutate({ item, changes: bindingDrafts[item.id] })}>
                  {t('settings.zigbee.sensors.saveBinding')}
                </button>
              </div>
            </div>;
          })}
          <div className="flex flex-wrap gap-2">
            <Select size="sm" aria-label={t('settings.zigbee.sensors.targetType')} value={targetType} onChange={(e) => {
              setTargetType(e.target.value as 'printer' | 'room' | 'storage'); setTargetId(null);
            }}>
              <option value="printer">{t('settings.zigbee.sensors.boundToPrinter')}</option>
              <option value="room">{t('settings.zigbee.sensors.boundToLocation')}</option>
              {hasPermission('inventory:read') && <option value="storage">{t('settings.zigbee.sensors.storage')}</option>}
            </Select>
            <Select size="sm" aria-label={t('settings.zigbee.sensors.pickTarget')} value={targetId ?? ''} onChange={(e) => setTargetId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">{t('settings.zigbee.sensors.pickTarget')}</option>
              {(targetType === 'printer' ? (printers ?? []).map((p) => ({ id: p.id, name: p.name }))
                : targetType === 'room' ? (rooms?.locations ?? []).map((p) => ({ id: p.id, name: p.path }))
                : (storage ?? []).map((p) => ({ id: p.id, name: p.name }))).map((p) => (
                  <option key={p.id} value={p.id}>{p.name}</option>
                ))}
            </Select>
          </div>
          <label className="flex items-center gap-2 text-sm text-bambu-gray">
            <input type="checkbox" checked={notifyEnabled} onChange={(e) => setNotifyEnabled(e.target.checked)} />
            {t('settings.zigbee.sensors.notifyBinding')}
          </label>
          <Button disabled={targetId == null || duplicateTarget || addBinding.isPending} onClick={() => addBinding.mutate()}>
            {t('settings.zigbee.sensors.addBinding')}
          </Button>
          {targetId != null && duplicateTarget &&
            <p className="text-sm text-status-error">{t('settings.zigbee.sensors.duplicateBinding')}</p>}
          {bindingError && <p className="text-sm text-status-error">{bindingError}</p>}
        </div>}

        {sensor === null && <div>
          <label className="block text-sm text-bambu-gray mb-1">{t('settings.zigbee.sensors.boundTo')}</label>
          {/* A choice, not a guess. An enclosure probe belongs to one machine
              and a room thermometer to the room; the hardware is identical, so
              only the operator knows which. Where the reading is drawn follows
              from this and nothing else. */}
          <div className="flex gap-1 mb-2" role="radiogroup" aria-label={t('settings.zigbee.sensors.boundTo')}>
            {(['location', 'printer', ...(hasPermission('inventory:read') ? ['storage' as const] : [])] as const).map((option) => (
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
                {option === 'storage' ? t('settings.zigbee.sensors.storage')
                  : t(`settings.zigbee.sensors.boundTo${option === 'location' ? 'Location' : 'Printer'}`)}
              </button>
            ))}
          </div>

          {boundTo === 'location' ? (
            <PrinterLocationSelect value={locationId} onChange={setLocationId} allowCreate />
          ) : boundTo === 'printer' ? (
            <Select
              size="sm"
              className="w-full"
              id="sensor-printer"
              aria-label={t('settings.zigbee.sensors.boundToPrinter')}
              value={printerId ?? ''}
              onChange={(e) => setPrinterId(e.target.value === '' ? null : Number(e.target.value))}
            >
              <option value="">{t('settings.zigbee.sensors.pickPrinter')}</option>
              {(printers ?? []).map((p) => (
                <option key={p.id} value={p.id}>
                  {p.name}
                </option>
              ))}
            </Select>
          ) : (
            <Select size="sm" className="w-full" aria-label={t('settings.zigbee.sensors.storage')}
              value={storageId ?? ''} onChange={e => setStorageId(e.target.value ? Number(e.target.value) : null)}>
              <option value="">{t('settings.zigbee.sensors.pickTarget')}</option>
              {(storage ?? []).map(item => <option key={item.id} value={item.id}>{item.name}</option>)}
            </Select>
          )}
          <p className="text-xs text-bambu-gray mt-1">
            {t(
              boundTo === 'location' ? 'settings.zigbee.sensors.boundToLocationHint'
                : boundTo === 'printer' ? 'settings.zigbee.sensors.boundToPrinterHint'
                  : 'sensorSettings.storageHint',
            )}
          </p>
        </div>}

        {save.isError && <p className="text-sm text-status-error" role="alert">{save.error.message}</p>}

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
    </Modal>
    {thresholdBinding && currentSensor && <SensorThresholdsModal isOpen sensor={currentSensor}
      bindingId={thresholdBinding.id}
      bindingName={thresholdBinding.printer_name || thresholdBinding.location?.path || thresholdBinding.storage_location_name || sensor?.name}
      onClose={() => setThresholdBinding(null)} />}
    {pendingRemoval != null && <Modal onClose={() => setPendingRemoval(null)}
      title={t('settings.zigbee.sensors.removeBinding')} size="sm">
      <div className="space-y-4 p-4">
        <p className="text-sm text-bambu-gray">{t('settings.zigbee.sensors.lastBindingWarning')}</p>
        <div className="flex justify-end gap-2">
          <Button variant="secondary" onClick={() => setPendingRemoval(null)}>{t('common.cancel')}</Button>
          <Button disabled={removeBinding.isPending} onClick={() => {
            removeBinding.mutate(pendingRemoval); setPendingRemoval(null);
          }}>{t('settings.zigbee.sensors.removeBinding')}</Button>
        </div>
      </div>
    </Modal>}
    </>
  );
}
