import { useTranslation } from 'react-i18next';
import { Cpu } from 'lucide-react';

import type { AddonInfo, PrinterSettingsGetResponse } from '../api/client';
import { getPrinterImage } from '../utils/printer';

interface Props {
  data: PrinterSettingsGetResponse;
  onRefetch: () => void;
}

// Accessory thumbnails, served statically from `public/img/addons/` — they are NOT
// bundled into the JS; the browser loads them from the server on demand, exactly like
// the printer images under `public/img/printers/`.
//
// EDIT THIS MAP to swap or rename accessory images: keys are the backend's `image_key`,
// values are the filenames sitting in `public/img/addons/`. To use your own picture,
// drop the file into that folder and point the key's value at it (svg or png both work).
// (The printer's OWN row does NOT use this map — see `imageFor` below.)
// Mirrored from BambuStudio — attribution in `public/img/addons/NOTICE.md`.
const ADDON_IMG_BASE = '/img/addons';

const ADDON_IMAGE_FILES: Record<string, string> = {
  // AMS + filament handling
  ams: 'ams.png',
  ams_lite: 'ams_lite.png',
  ams_ht: 'ams_ht.png',
  air_pump: 'air_pump.png',
  filament_buffer_p2s: 'filament_buffer_p2s.png',
  filament_buffer_x2d: 'filament_buffer_x2d.png',
  filament_track: 'filament_track.png',
  // Accessories
  cutting: 'cutting.png',
  exhaust_fan: 'exhaust_fan.png',
  ext: 'ext.png',
  extinguish: 'extinguish.png',
  laser: 'laser.png',
  laser_40: 'laser_40.png',
  nozzle_rack: 'nozzle_rack.png',
  rotary: 'rotary.png',
};

function imageFor(key: string): string | undefined {
  // The printer's own row (`image_key` = "printer_<slug>") reuses the SHARED printer
  // thumbnails under public/img/printers/ — the same images as the printer cards, via
  // getPrinterImage — so there is no duplicate copy. "printer_generic" (unknown model)
  // uses the generic printer illustration moved into public/img/printers/generic.svg.
  if (key.startsWith('printer_')) {
    if (key === 'printer_generic') return '/img/printers/generic.svg';
    return getPrinterImage(key.slice('printer_'.length)); // slug e.g. "x2d" → /img/printers/x2d.png
  }
  const file = ADDON_IMAGE_FILES[key];
  return file ? `${ADDON_IMG_BASE}/${file}` : undefined;
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
