import { useTranslation } from 'react-i18next';

import type {
  CalibCapabilities,
  CalibModeState,
  CaliMethod,
  CaliMode,
} from '../../api/client';

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

function resolveModeState(
  capabilities: CalibCapabilities | undefined,
  mode: CaliMode,
): CalibModeState {
  // Auto-paths read by their CaliMode value: 'flow_rate' for both manual
  // and auto flow rate (MQTT-side dispatch is gated on capability flag,
  // not on a separate enum). Auto PA is 'auto_pa_line'. The server's
  // mode_state map keys on CaliMode so both 'flow_rate' rows share the
  // same lifecycle entry — disabling 'flow_rate' disables both.
  return capabilities?.mode_state?.[mode] ?? 'disabled';
}

export function CalibrationStartPage({ capabilities, onPick }: Props) {
  const { t } = useTranslation();

  const renderRow = (r: OptionRow) => {
    const supported = capabilities ? Boolean(capabilities[r.capKey]) : false;
    const state = resolveModeState(capabilities, r.mode);
    // A row is interactive only when the per-printer capability flag AND
    // the global mode_state agree it's usable. 'disabled' blocks the
    // click outright; 'verification' lets the click through so the
    // confirm page can render the "Download sliced 3MF" button.
    const interactive = supported && state !== 'disabled';
    const reason = !supported
      ? t('filamentCali.start.notSupported')
      : state === 'disabled'
        ? t('filamentCali.start.notImplemented')
        : '';
    const pill =
      state === 'verification' ? (
        <span
          className="text-[10px] font-semibold uppercase tracking-wide bg-yellow-500/20 text-yellow-300 px-1.5 py-0.5 rounded"
          title={t('filamentCali.start.verificationModeTooltip')}
        >
          {t('filamentCali.start.verificationModePill')}
        </span>
      ) : null;

    return (
      <button
        key={`${r.mode}-${r.method}`}
        type="button"
        onClick={() => interactive && onPick(r.mode, r.method)}
        disabled={!interactive}
        title={reason}
        className={`w-full text-left p-3 rounded-lg border transition-colors ${
          !interactive
            ? 'border-bambu-dark-tertiary bg-bambu-dark opacity-50 cursor-not-allowed'
            : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green'
        }`}
      >
        <div className="flex items-center justify-between gap-2">
          <span className="font-medium text-white flex items-center gap-2">
            {t(`filamentCali.start.${r.labelKey}`)}
            {pill}
          </span>
          {!interactive && reason && (
            <span className="text-xs text-bambu-gray">{reason}</span>
          )}
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
