import { useTranslation } from 'react-i18next';
import { Cpu } from 'lucide-react';

import type { AddonInfo, PrinterSettingsGetResponse } from '../api/client';

interface Props {
  data: PrinterSettingsGetResponse;
  onRefetch: () => void;
}

// Bundle every add-on image (printer thumbnails + accessory icons) mirrored from
// BambuStudio under assets/addons. Keyed by filename stem == the backend's
// `image_key` (e.g. "printer_x2d", "ams", "exhaust_fan").
const _addonImageModules = import.meta.glob('../assets/addons/*.{svg,png}', {
  eager: true,
  query: '?url',
  import: 'default',
}) as Record<string, string>;

const ADDON_IMAGES: Record<string, string> = {};
for (const [path, url] of Object.entries(_addonImageModules)) {
  const stem = path.split('/').pop()?.replace(/\.(svg|png)$/, '');
  if (stem) ADDON_IMAGES[stem] = url;
}

function imageFor(key: string): string | undefined {
  return ADDON_IMAGES[key] ?? (key.startsWith('printer_') ? ADDON_IMAGES['printer_generic'] : undefined);
}

export function PrinterAddonsTab({ data, onRefetch }: Props) {
  const { t } = useTranslation();
  const addons = data.addons ?? [];

  if (addons.length === 0) {
    return <div className="text-bambu-gray">{t('printerSettings.waitingForPrinter')}</div>;
  }

  return (
    <div className="space-y-4">
      <div className="space-y-2">
        {addons.map((a) => (
          <AddonRow key={`${a.name}-${a.display_name}`} addon={a} />
        ))}
      </div>
      <button
        type="button"
        className="px-3 py-1 bg-bambu-dark hover:bg-bambu-dark-tertiary rounded text-white text-sm"
        onClick={onRefetch}
      >
        {t('printerSettings.parts.refresh')}
      </button>
    </div>
  );
}

function AddonRow({ addon }: { addon: AddonInfo }) {
  const { t } = useTranslation();
  const img = imageFor(addon.image_key);
  return (
    <div className="flex items-center gap-3 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark p-3">
      <div className="flex h-14 w-14 flex-shrink-0 items-center justify-center">
        {img ? (
          <img src={img} alt="" className="max-h-14 max-w-14 object-contain" />
        ) : (
          <Cpu className="h-8 w-8 text-bambu-gray" />
        )}
      </div>
      <div className="min-w-0 flex-1">
        <div className="truncate font-medium text-white">{addon.display_name}</div>
        <div className="mt-0.5 flex flex-wrap gap-x-4 gap-y-0.5 text-xs text-bambu-gray">
          {addon.sw_ver && (
            <span>
              {t('printerSettings.addons.firmware')}: <span className="text-bambu-light">{addon.sw_ver}</span>
            </span>
          )}
          {addon.hw_ver && (
            <span>
              {t('printerSettings.addons.hardware')}: <span className="text-bambu-light">{addon.hw_ver}</span>
            </span>
          )}
          {addon.serial && (
            <span>
              {t('printerSettings.addons.serial')}: <span className="text-bambu-light">{addon.serial}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
