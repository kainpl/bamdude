import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Settings, ChevronDown, ChevronUp, Flame } from 'lucide-react';
import type { PrintOptionsProps, PrintOptions as PrintOptionsType, PreheatOverride } from './types';

const PRINT_OPTIONS_CONFIG = [
  { key: 'bed_levelling', labelKey: 'printModal.bedLeveling', descKey: 'printModal.bedLevelingDesc' },
  { key: 'flow_cali', labelKey: 'printModal.flowCalibration', descKey: 'printModal.flowCalibrationDesc' },
  { key: 'layer_inspect', labelKey: 'printModal.layerInspection', descKey: 'printModal.layerInspectionDesc' },
  { key: 'timelapse', labelKey: 'printModal.timelapse', descKey: 'printModal.timelapseDesc' },
  { key: 'mesh_mode_fast_check', labelKey: 'printModal.meshModeFastCheck', descKey: 'printModal.meshModeFastCheckDesc' },
  { key: 'gcode_injection', labelKey: 'printModal.gcodeInjection', descKey: 'printModal.gcodeInjectionDesc' },
] as const;

// Dual-nozzle-only options (H2D/H2D Pro/H2C/X2D) — appended to the panel only
// when the selected printer(s) are dual-nozzle (#1682). The MQTT layer forces
// "skip" on single-nozzle machines regardless, so hiding it here just avoids a
// misleading no-op toggle.
const DUAL_NOZZLE_OPTIONS_CONFIG = [
  { key: 'nozzle_offset_cali', labelKey: 'printModal.nozzleOffsetCali', descKey: 'printModal.nozzleOffsetCaliDesc' },
] as const;

/**
 * Print options toggle panel with collapsible UI.
 * Shows bed levelling, flow/vibration calibration, layer inspection, and timelapse options.
 */
export function PrintOptionsPanel({
  options,
  onChange,
  defaultExpanded = false,
  showDualNozzleOptions = false,
}: PrintOptionsProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  const handleToggle = (key: keyof PrintOptionsType) => {
    onChange({ ...options, [key]: !options[key] });
  };

  const handlePreheatOverride = (next: PreheatOverride) => {
    onChange({
      ...options,
      preheat_override: next,
      // Clearing override→off also clears the chamber-target override so the
      // backend doesn't carry a stale value if the user re-enables later.
      ...(next === 'off' ? { preheat_chamber_target_override: null } : {}),
    });
  };

  const handlePreheatTarget = (raw: string) => {
    if (raw === '') {
      onChange({ ...options, preheat_chamber_target_override: null });
      return;
    }
    const parsed = parseInt(raw, 10);
    if (Number.isNaN(parsed)) return;
    onChange({
      ...options,
      preheat_chamber_target_override: Math.max(0, Math.min(60, parsed)),
    });
  };

  const visibleOptions = showDualNozzleOptions
    ? [...PRINT_OPTIONS_CONFIG, ...DUAL_NOZZLE_OPTIONS_CONFIG]
    : PRINT_OPTIONS_CONFIG;

  return (
    <div className="mb-4">
      <button
        type="button"
        onClick={() => setIsExpanded(!isExpanded)}
        className="flex items-center gap-2 text-sm text-bambu-gray hover:text-white transition-colors w-full"
      >
        <Settings className="w-4 h-4" />
        <span>{t('printModal.printOptions')}</span>
        {isExpanded ? (
          <ChevronUp className="w-4 h-4 ml-auto" />
        ) : (
          <ChevronDown className="w-4 h-4 ml-auto" />
        )}
      </button>
      {isExpanded && (
        <div className="mt-2 bg-bambu-dark rounded-lg p-3 space-y-2">
          {visibleOptions.map(({ key, labelKey, descKey }) => (
            <label key={key} className="flex items-center justify-between gap-3 cursor-pointer group">
              <div className="min-w-0 flex-1">
                <span className="text-sm text-white">{t(labelKey)}</span>
                <p className="text-xs text-bambu-gray">{t(descKey)}</p>
              </div>
              <div
                className={`relative w-10 h-5 rounded-full transition-colors flex-shrink-0 ${
                  options[key] ? 'bg-bambu-green' : 'bg-bambu-dark-tertiary'
                }`}
                onClick={() => handleToggle(key)}
              >
                <div
                  className={`absolute top-0.5 w-4 h-4 rounded-full bg-white transition-transform ${
                    options[key] ? 'translate-x-5' : 'translate-x-0.5'
                  }`}
                />
              </div>
            </label>
          ))}

          {/* Preheat / heat-soak per-item override (#1468). Defaults to
              'inherit' which means the global Settings → Printing toggle
              decides. Forcing 'on' or 'off' overrides per-print; the chamber
              target override (optional °C input, visible when not 'off')
              bypasses the per-filament-type derivation. Bed preheat + soak
              applies to every model — only the chamber phase is model-gated,
              and that gating lives server-side — so this sub-section renders
              for all printers regardless of chamber capability. */}
          <div className="pt-2 mt-1 border-t border-bambu-dark-tertiary/60">
            <div className="flex items-center gap-2 mb-1.5">
              <Flame className="w-3.5 h-3.5 text-amber-400" />
              <span className="text-sm text-white">{t('settings.preheatTitle')}</span>
            </div>
            <p className="text-xs text-bambu-gray mb-2">
              {t('settings.preheatPerItemDesc')}
            </p>
            <div className="flex gap-1.5 mb-2">
              {(['inherit', 'on', 'off'] as PreheatOverride[]).map((opt) => (
                <button
                  key={opt}
                  type="button"
                  onClick={() => handlePreheatOverride(opt)}
                  className={`flex-1 px-2 py-1.5 text-xs rounded transition-colors ${
                    options.preheat_override === opt
                      ? 'bg-bambu-green text-white'
                      : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white'
                  }`}
                >
                  {t(`settings.preheatOverride_${opt}`)}
                </button>
              ))}
            </div>
            {options.preheat_override !== 'off' && (
              <div className="flex items-center gap-2">
                <label className="text-xs text-bambu-gray flex-1">
                  {t('settings.preheatTargetOverride')}
                </label>
                <input
                  type="number"
                  min={0}
                  max={60}
                  step={1}
                  value={options.preheat_chamber_target_override ?? ''}
                  onChange={(e) => handlePreheatTarget(e.target.value)}
                  placeholder="—"
                  className="w-16 px-2 py-1 bg-bambu-dark-tertiary border border-bambu-dark-tertiary rounded text-white text-xs text-right focus:outline-none focus:border-bambu-green"
                />
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
