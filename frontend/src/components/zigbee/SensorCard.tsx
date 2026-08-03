import { useTranslation } from 'react-i18next';
import { Battery, Pencil, Plug, SlidersHorizontal, Trash2, WifiOff } from 'lucide-react';

import type { SensorMeasurement, ZigbeeSensor } from '../../api/client';
import { formatRelativeTime } from '../../utils/date';

interface Props {
  sensor: ZigbeeSensor;
  onEdit: (sensor: ZigbeeSensor) => void;
  onUnbind: (sensor: ZigbeeSensor) => void;
  onConfigure: (sensor: ZigbeeSensor) => void;
  canEdit: boolean;
  canDelete: boolean;
  canConfigure: boolean;
}

/** Kept out of the readings list: they describe the device, not the room. */
const DEVICE_KEYS = ['battery', 'battery_voltage'];

/**
 * One sensor, mirroring SmartPlugCard.
 *
 * Four bad states are kept apart because the backend distinguishes them and
 * folding them into one "offline" throws that away:
 *
 *   radio down       - a banner above the whole section, not here
 *   present: false   - the device is not on the mesh; our row survives
 *   unreachable      - on the mesh, silent past the point we vouch for it
 *   stale            - one quantity is older than its own window
 *
 * The last two differ in subject. A sensor can be answering while its battery
 * reading is stale, because that one reports every three hours.
 */
export function SensorCard({ sensor, onEdit, onUnbind, onConfigure, canEdit, canDelete, canConfigure }: Props) {
  const { t } = useTranslation();

  const readings = Object.entries(sensor.measurements).filter(([key]) => !DEVICE_KEYS.includes(key));
  const battery = sensor.measurements.battery;
  const voltage = sensor.measurements.battery_voltage;

  return (
    <div className="bg-bambu-dark-secondary rounded-xl p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-white truncate">{sensor.name}</p>
          <p className="text-xs text-bambu-gray truncate">
            {[sensor.location?.name, sensor.model || sensor.ieee].filter(Boolean).join(' · ')}
          </p>
        </div>

        <div className="flex items-center gap-2 shrink-0">
          {sensor.power === 'mains' ? (
            <span
              className="text-xs text-bambu-gray flex items-center gap-1"
              title={t('settings.zigbee.sensors.mainsPowered')}
            >
              <Plug className="w-3.5 h-3.5" />
              {t('settings.zigbee.sensors.mainsPowered')}
            </span>
          ) : battery?.value != null ? (
            <span
              className={`text-xs flex items-center gap-1 ${battery.stale ? 'text-bambu-gray/50' : 'text-bambu-gray'}`}
              // Voltage only in the tooltip: it is interesting once you already
              // suspect the cell. A stale battery is muted with no "N ago" --
              // at a three-hour interval "2 hours ago" is normal, not a signal.
              title={
                voltage?.value != null
                  ? t('settings.zigbee.sensors.batteryVoltage', { volts: voltage.value })
                  : undefined
              }
            >
              <Battery className="w-3.5 h-3.5" />
              {t('settings.zigbee.sensors.battery', { percent: Math.round(battery.value) })}
            </span>
          ) : null}

          {canConfigure && (
            <button
              type="button"
              onClick={() => onConfigure(sensor)}
              aria-label={t('settings.zigbee.reporting.title')}
              className="p-1 text-bambu-gray hover:text-white"
            >
              <SlidersHorizontal className="w-4 h-4" />
            </button>
          )}
          {canEdit && (
            <button
              type="button"
              onClick={() => onEdit(sensor)}
              aria-label={t('common.edit')}
              className="p-1 text-bambu-gray hover:text-white"
            >
              <Pencil className="w-4 h-4" />
            </button>
          )}
          {canDelete && (
            <button
              type="button"
              onClick={() => onUnbind(sensor)}
              aria-label={t('common.delete')}
              className="p-1 text-bambu-gray hover:text-status-error"
            >
              <Trash2 className="w-4 h-4" />
            </button>
          )}
        </div>
      </div>

      {!sensor.present ? (
        <div className="mt-3 text-sm text-amber-600 dark:text-amber-400 flex items-start gap-2">
          <WifiOff className="w-4 h-4 mt-0.5 shrink-0" />
          <span>
            {t('settings.zigbee.sensors.notOnNetwork')}
            <span className="block text-xs text-bambu-gray">{t('settings.zigbee.sensors.notOnNetworkHint')}</span>
          </span>
        </div>
      ) : (
        <>
          {sensor.unreachable && (
            <p className="mt-3 text-sm text-amber-600 dark:text-amber-400">
              {t('settings.zigbee.sensors.notAnswering')}
            </p>
          )}
          <ul className="mt-3 space-y-1">
            {readings.map(([key, reading]) => (
              <Reading key={key} name={key} reading={reading} muted={sensor.unreachable} />
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function Reading({ name, reading, muted }: { name: string; reading: SensorMeasurement; muted: boolean }) {
  const { t } = useTranslation();
  // Muted for a value we still have but no longer vouch for -- either the whole
  // device went quiet or this one quantity is past its window.
  const dim = muted || reading.stale;

  return (
    <li className="flex items-center justify-between gap-3 text-sm">
      <span className="text-bambu-gray">{t(`settings.zigbee.measurement.${name}`, { defaultValue: name })}</span>
      <span className={dim ? 'text-bambu-gray' : 'text-white'}>
        {reading.value == null ? '—' : `${reading.value} ${reading.unit}`}
      </span>
      {/* Always shown, stale or not: "2 min ago" is as much a fact as "14 min
          ago". Staleness changes the emphasis, not the presence of the time. */}
      <span className="text-xs text-bambu-gray shrink-0">
        {formatRelativeTime(reading.last_report_at, 'system', t)}
      </span>
    </li>
  );
}
