import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Settings, ChevronDown, ChevronUp } from 'lucide-react';
import type { PrintOptionsProps, PrintOptions as PrintOptionsType } from './types';

const PRINT_OPTIONS_CONFIG = [
  { key: 'bed_levelling', labelKey: 'printModal.bedLeveling', descKey: 'printModal.bedLevelingDesc' },
  { key: 'flow_cali', labelKey: 'printModal.flowCalibration', descKey: 'printModal.flowCalibrationDesc' },
  { key: 'layer_inspect', labelKey: 'printModal.layerInspection', descKey: 'printModal.layerInspectionDesc' },
  { key: 'timelapse', labelKey: 'printModal.timelapse', descKey: 'printModal.timelapseDesc' },
  { key: 'mesh_mode_fast_check', labelKey: 'printModal.meshModeFastCheck', descKey: 'printModal.meshModeFastCheckDesc' },
  { key: 'gcode_injection', labelKey: 'printModal.gcodeInjection', descKey: 'printModal.gcodeInjectionDesc' },
] as const;

/**
 * Print options toggle panel with collapsible UI.
 * Shows bed levelling, flow/vibration calibration, layer inspection, and timelapse options.
 */
export function PrintOptionsPanel({
  options,
  onChange,
  defaultExpanded = false,
}: PrintOptionsProps) {
  const { t } = useTranslation();
  const [isExpanded, setIsExpanded] = useState(defaultExpanded);

  const handleToggle = (key: keyof PrintOptionsType) => {
    onChange({ ...options, [key]: !options[key] });
  };

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
          {PRINT_OPTIONS_CONFIG.map(({ key, labelKey, descKey }) => (
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
        </div>
      )}
    </div>
  );
}
