import { useTranslation } from 'react-i18next';

import type { CalibCapabilities, CaliMethod, CaliMode } from '../../api/client';

interface Props {
  capabilities: CalibCapabilities | undefined;
  onPick: (mode: CaliMode, method: CaliMethod) => void;
}

interface OptionRow {
  mode: CaliMode;
  method: CaliMethod;
  labelKey: string;
  descKey: string;
  capKey: keyof CalibCapabilities;
}

const PA_OPTIONS: OptionRow[] = [
  { mode: 'pa_line', method: 'manual', labelKey: 'paLine', descKey: 'paLineDesc', capKey: 'pa_manual' },
  { mode: 'pa_pattern', method: 'manual', labelKey: 'paPattern', descKey: 'paPatternDesc', capKey: 'pa_manual' },
  { mode: 'pa_tower', method: 'manual', labelKey: 'paTower', descKey: 'paTowerDesc', capKey: 'pa_manual' },
  { mode: 'auto_pa_line', method: 'auto', labelKey: 'paAuto', descKey: 'paAutoDesc', capKey: 'pa_auto' },
];

const FLOW_OPTIONS: OptionRow[] = [
  { mode: 'flow_rate', method: 'manual', labelKey: 'flowRate', descKey: 'flowRateDesc', capKey: 'flow_manual' },
  { mode: 'flow_rate', method: 'auto', labelKey: 'flowAuto', descKey: 'flowAutoDesc', capKey: 'flow_auto' },
];

const TOWER_OPTIONS: OptionRow[] = [
  { mode: 'temp_tower', method: 'manual', labelKey: 'tempTower', descKey: 'tempTowerDesc', capKey: 'temp_tower' },
  { mode: 'vol_speed_tower', method: 'manual', labelKey: 'volSpeedTower', descKey: 'volSpeedTowerDesc', capKey: 'vol_speed_tower' },
  { mode: 'vfa_tower', method: 'manual', labelKey: 'vfaTower', descKey: 'vfaTowerDesc', capKey: 'vfa_tower' },
  { mode: 'retraction_tower', method: 'manual', labelKey: 'retractionTower', descKey: 'retractionTowerDesc', capKey: 'retraction_tower' },
];

export function CalibrationStartPage({ capabilities, onPick }: Props) {
  const { t } = useTranslation();

  const renderRow = (r: OptionRow) => {
    const supported = capabilities ? Boolean(capabilities[r.capKey]) : false;
    const disabled = !supported;
    const reason = !supported ? t('filamentCali.start.notSupported') : '';
    return (
      <button
        key={`${r.mode}-${r.method}`}
        type="button"
        onClick={() => !disabled && onPick(r.mode, r.method)}
        disabled={disabled}
        title={reason}
        className={`w-full text-left p-3 rounded-lg border transition-colors ${
          disabled
            ? 'border-bambu-dark-tertiary bg-bambu-dark opacity-50 cursor-not-allowed'
            : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green'
        }`}
      >
        <div className="flex items-center justify-between">
          <span className="font-medium text-white">{t(`filamentCali.start.${r.labelKey}`)}</span>
          {disabled && <span className="text-xs text-bambu-gray">{reason}</span>}
        </div>
        <p className="text-sm text-bambu-gray mt-1">{t(`filamentCali.start.${r.descKey}`)}</p>
      </button>
    );
  };

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.start.heading')}</h3>

      <section>
        <h4 className="text-sm font-medium text-bambu-gray mb-2">{t('filamentCali.start.paGroup')}</h4>
        <div className="space-y-2">{PA_OPTIONS.map(renderRow)}</div>
      </section>

      <section>
        <h4 className="text-sm font-medium text-bambu-gray mb-2">{t('filamentCali.start.flowGroup')}</h4>
        <div className="space-y-2">{FLOW_OPTIONS.map(renderRow)}</div>
      </section>

      <section>
        <h4 className="text-sm font-medium text-bambu-gray mb-2">{t('filamentCali.start.towerGroup')}</h4>
        <div className="space-y-2">{TOWER_OPTIONS.map(renderRow)}</div>
      </section>
    </div>
  );
}
