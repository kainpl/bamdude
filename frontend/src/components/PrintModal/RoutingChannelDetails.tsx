import { useTranslation } from 'react-i18next';
import type { PrinterRoutingChannel } from '../../api/client';
import { formatSlotLabel } from '../../utils/amsHelpers';

function slotLabel(id: number, dualNozzle: boolean) {
  if (id === 255) return 'Ext-R';
  if (id === 254) return dualNozzle ? 'Ext-L' : 'Ext';
  if (id >= 24 && id <= 27) return `Lite-${(id % 4) + 1}`;
  return formatSlotLabel(id >= 128 ? id : Math.floor(id / 4), id % 4, id >= 128, false);
}

function describe(material: string, profile: string | null) {
  return profile ? `${material} (${profile})` : material;
}

export function RoutingChannelDetails({ channel, dualNozzle }: { channel: PrinterRoutingChannel; dualNozzle: boolean }) {
  const { t } = useTranslation();
  const selected = channel.sources.find(source => source.source_id === channel.selected_source_id);
  const wanted = describe(channel.material, channel.profile);
  const details = channel.sources.map(source => {
    const compared = describe(source.material, source.profile);
    const reported = source.reported_material && source.reported_material !== source.material
      ? t('filamentRouting.channelReportedMaterial', { material: source.reported_material })
      : null;
    const label = `${slotLabel(source.source_id, dualNozzle)} · ${compared}${reported ? ` · ${reported}` : ''}`;
    return { ...source, label };
  });
  const chosen = details.find(source => source.source_id === channel.selected_source_id);
  return (
    <div className="pl-5 text-xs text-bambu-gray" data-testid="routing-channel-detail">
      {chosen ? (
        <p className={chosen.reason ? 'text-orange-700 dark:text-orange-400' : undefined}>
          {chosen.reason
            ? t('filamentRouting.channelSourceRefusal', { required: wanted, source: chosen.label, reason: chosen.reason.message })
            : chosen.label}
        </p>
      ) : channel.reason && (
        <p className="text-orange-700 dark:text-orange-400">
          {channel.selected_source_id != null && `${slotLabel(channel.selected_source_id, dualNozzle)} · `}
          {channel.reason.message}
        </p>
      )}
      {(!selected || details.length > 1) && details.length > 0 && (
        <details className="mt-1">
          <summary className="cursor-pointer">{t('filamentRouting.channelCheckedSources', { required: wanted })}</summary>
          <ul className="mt-1 space-y-1 pl-3">
            {details.map(source => (
              <li key={source.source_id}>
                {source.label}{' · '}{source.reason?.message ?? t('filamentRouting.channelSourceCompatible')}
              </li>
            ))}
          </ul>
          {channel.omitted_sources > 0 && (
            <p className="pl-3">{t('filamentRouting.channelOmittedSources', { count: channel.omitted_sources })}</p>
          )}
        </details>
      )}
    </div>
  );
}
