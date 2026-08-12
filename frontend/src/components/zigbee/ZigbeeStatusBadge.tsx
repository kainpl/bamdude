/**
 * Says the Zigbee radio is down, and says nothing otherwise.
 *
 * A dead radio makes every Zigbee plug stop answering at once, so without this
 * the operator sees "offline" in four places and a cause in none of them. The
 * `zigbee_status_changed` toast does not cover it: that fires on a *change*, so a
 * dongle already unplugged when BamDude started produces no event at all — which
 * is exactly when an explanation is needed.
 *
 * Rendering nothing when the radio is fine, disabled or still starting is the
 * point, not an omission. A permanent indicator about a feature an install does
 * not use is noise, and noise is what gets ignored.
 */

import { useTranslation } from 'react-i18next';
import { WifiOff } from 'lucide-react';

import { useZigbeeStatus } from './useZigbeeStatus';

interface ZigbeeStatusBadgeProps {
  variant: 'dot' | 'inline';
}

export function ZigbeeStatusBadge({ variant }: ZigbeeStatusBadgeProps) {
  const { t } = useTranslation();
  const { status, isDown } = useZigbeeStatus();

  if (!isDown) return null;

  // Verbatim, never mapped: the reason is the only part that says what to do
  // ("port busy — Zigbee2MQTT or Home Assistant is the most likely owner").
  const explanation = status?.reason || t('settings.zigbee.radioDownShort');

  if (variant === 'dot') {
    return (
      <span
        title={explanation}
        aria-label={explanation}
        className="inline-block w-2 h-2 rounded-full bg-red-500 shrink-0"
      />
    );
  }

  return (
    <span className="flex items-start gap-1 text-xs text-red-600 dark:text-red-400">
      <WifiOff className="w-3.5 h-3.5 shrink-0 mt-0.5" />
      <span>{explanation}</span>
    </span>
  );
}
