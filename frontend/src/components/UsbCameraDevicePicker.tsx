import { useId, useState } from 'react';
import { RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { UsbCameraListing } from '../api/client';
import { Button } from './Button';
import { Select } from './Select';

function devicePath(value: string) {
  const match = /^\/dev\/video([0-9]{1,2})$/.exec(value.trim());
  return match ? `/dev/video${Number(match[1])}` : '';
}

interface UsbCameraDevicePickerProps {
  printerName: string;
  value: string;
  stored: boolean;
  listing: UsbCameraListing | undefined;
  printerId: number;
  isFetching: boolean;
  isError: boolean;
  onRefresh: () => void;
  onSelect: (path: string) => void;
}

export function UsbCameraDevicePicker({
  printerName, value, stored, listing, printerId, isFetching, isError, onRefresh, onSelect,
}: UsbCameraDevicePickerProps) {
  const { t } = useTranslation();
  const selectId = useId();
  const manualId = useId();
  const [manual, setManual] = useState(false);
  const [draft, setDraft] = useState('');
  const devices = listing?.cameras ?? [];
  // A hidden credential from another camera transport must stay hidden even
  // while a printer type change and an unsaved URL draft overlap.
  const device = devicePath(value);
  const manualDevice = devicePath(draft);
  const detected = devices.some((item) => item.device === device);
  const found = listing?.configured_devices_found?.[printerId];
  const showMissing = stored && !isFetching && !isError && found === false;

  return (
    <div className="space-y-2">
      <label className="block text-xs text-bambu-gray" htmlFor={selectId}>
        {t('settings.usbCamera.detected')} · {printerName}
      </label>
      <div className="flex flex-wrap items-center gap-2">
        <Select
          id={selectId}
          tone="raised"
          className="min-w-0 flex-1"
          value={device && detected ? device : ''}
          onChange={(event) => onSelect(event.target.value)}
        >
          <option value="" disabled>
            {stored && !device ? t('settings.usbCamera.stored') : t('settings.usbCamera.choose')}
          </option>
          {devices.map(({ device: path, name }) => (
            <option key={path} value={path}>{name} · {path}</option>
          ))}
        </Select>
        <Button size="sm" variant="secondary" disabled={isFetching} onClick={onRefresh}>
          <RefreshCw className={`w-4 h-4 ${isFetching ? 'animate-spin' : ''}`} />
          {t('common.refresh')}
        </Button>
      </div>
      {isFetching && <p role="status" className="text-xs text-bambu-gray">{t('settings.usbCamera.scanning')}</p>}
      {isError && <p role="status" className="text-xs text-bambu-gray">{t('settings.usbCamera.failed')}</p>}
      {!isFetching && !isError && listing && devices.length === 0 && (
        <p role="status" className="text-xs text-bambu-gray">{t('settings.usbCamera.empty')}</p>
      )}
      {showMissing && <p role="status" className="text-xs text-yellow-600 dark:text-yellow-400">{t('settings.usbCamera.missing')}</p>}
      <Button size="sm" variant="ghost" onClick={() => {
        setDraft(device);
        setManual(!manual);
      }}>
        {t(manual ? 'settings.usbCamera.hideManual' : 'settings.usbCamera.manual')}
      </Button>
      {manual && (
        <div>
          <label htmlFor={manualId} className="block text-xs text-bambu-gray mb-1">
            {t('settings.cameraPlaceholderUsb')}
          </label>
          <div className="flex flex-wrap gap-2">
            <input
              id={manualId}
              placeholder="/dev/video0"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              className="min-w-0 flex-1 px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-sm"
            />
            <Button size="sm" disabled={!manualDevice} onClick={() => {
              onSelect(manualDevice);
              setManual(false);
            }}>
              {t('common.save')}
            </Button>
          </div>
        </div>
      )}
      <p className="text-xs text-bambu-gray">{t('settings.usbCamera.docker')}</p>
    </div>
  );
}
