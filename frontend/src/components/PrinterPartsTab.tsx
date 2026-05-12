import { useTranslation } from 'react-i18next';

import type { PrinterSettingsGetResponse } from '../api/client';

interface Props {
  data: PrinterSettingsGetResponse;
  onRefetch: () => void;
}

export function PrinterPartsTab({ data, onRefetch }: Props) {
  const { t } = useTranslation();
  const nozzles = data.parts.nozzles ?? [];
  const dual = data.supports.parts_dual;

  if (nozzles.length === 0) {
    return <div className="text-bambu-gray">{t('printerSettings.waitingForPrinter')}</div>;
  }

  return (
    <div className="space-y-4">
      <div className={dual ? 'grid grid-cols-2 gap-6' : ''}>
        {nozzles.map((n) => (
          <NozzleCard
            key={n.id}
            label={dual ? (n.id === 0 ? t('printerSettings.parts.leftNozzle') : t('printerSettings.parts.rightNozzle')) : null}
            type={n.type}
            diameter={n.diameter}
            flowType={n.flow_type}
          />
        ))}
      </div>
      <p className="text-sm text-bambu-gray">
        {t('printerSettings.parts.changeOnPrinter')}
      </p>
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

function NozzleCard({ label, type, diameter, flowType }: {
  label: string | null;
  type: string | null;
  diameter: number | null;
  flowType: string | null;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      {label && <div className="text-white font-medium">{label}</div>}
      <ReadOnlyRow label={t('printerSettings.parts.type')} value={type ?? '—'} />
      <ReadOnlyRow label={t('printerSettings.parts.diameter')} value={diameter != null ? String(diameter) : '—'} />
      <ReadOnlyRow label={t('printerSettings.parts.flow')} value={flowType ?? '—'} />
    </div>
  );
}

function ReadOnlyRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-center gap-3">
      <div className="text-bambu-gray text-sm w-24">{label}</div>
      <div className="flex-1 bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1 text-white opacity-70">
        {value}
      </div>
    </div>
  );
}
