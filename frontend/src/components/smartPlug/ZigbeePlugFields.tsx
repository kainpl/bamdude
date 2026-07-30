/**
 * Pick a paired Zigbee device for a plug.
 *
 * Its own file rather than a sixth nested branch inside AddSmartPlugModal.tsx,
 * which is already past 1800 lines.
 *
 * Deliberately has **no** multiplier or divisor inputs, unlike the MQTT and REST
 * field sets. The device reports its own scaling on the Metering cluster, so
 * asking an operator for it would invite a wrong answer where a right one is
 * already available — and a wrong scale turns kWh into a number off by a factor
 * of a thousand that still looks reasonable.
 */

import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api } from '../../api/client';

interface ZigbeePlugFieldsProps {
  value: string | null;
  onChange: (ieee: string | null) => void;
  /** IEEEs already taken by another plug. Compared case-insensitively: zigpy
   *  stringifies EUI64 lower-case, but an operator may have pasted upper. */
  excludeIeees: string[];
}

export function ZigbeePlugFields({ value, onChange, excludeIeees }: ZigbeePlugFieldsProps) {
  const { t } = useTranslation();

  const { data: status } = useQuery({ queryKey: ['zigbee-status'], queryFn: api.getZigbeeStatus });
  const { data: deviceList } = useQuery({ queryKey: ['zigbee-devices'], queryFn: api.getZigbeeDevices });

  const radioUp = status?.state === 'up';
  const taken = new Set(excludeIeees.map((ieee) => ieee.toLowerCase()));
  const available = (deviceList?.devices ?? []).filter(
    (device) => device.is_plug && !taken.has(device.ieee.toLowerCase()),
  );

  const selected = available.find((device) => device.ieee.toLowerCase() === (value ?? '').toLowerCase());
  const reportsEnergy = selected ? selected.has_metering || selected.has_electrical_measurement : true;

  return (
    <div>
      <label className="block text-sm text-bambu-gray mb-1">{t('smartPlugs.zigbeeDevice')}</label>
      <select
        value={value ?? ''}
        onChange={(e) => onChange(e.target.value || null)}
        disabled={!radioUp || available.length === 0}
        className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white focus:border-bambu-green focus:outline-none disabled:opacity-50"
      >
        <option value="">{t('smartPlugs.zigbeePickDevice')}</option>
        {available.map((device) => (
          <option key={device.ieee} value={device.ieee}>
            {device.model ? `${device.model} — ${device.ieee}` : device.ieee}
          </option>
        ))}
      </select>

      {/* Each disabled state says which one it is and where to go, rather than
          leaving the operator with a greyed-out control and no explanation. */}
      {!radioUp ? (
        <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">{t('smartPlugs.zigbeeCoordinatorDown')}</p>
      ) : available.length === 0 ? (
        <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">{t('smartPlugs.zigbeeNoDevices')}</p>
      ) : (
        <p className="text-xs text-bambu-gray mt-1">{t('smartPlugs.zigbeeDeviceHint')}</p>
      )}

      {/* The backend accepts a plug with no metering deliberately — it can still
          be switched. Saying so up front is required, because the alternative is
          a consumer reading an absent value as a measured zero. */}
      {selected && !reportsEnergy ? (
        <p className="text-xs text-amber-600 dark:text-amber-400 mt-1">{t('smartPlugs.zigbeeNoMeteringWarning')}</p>
      ) : null}
    </div>
  );
}
