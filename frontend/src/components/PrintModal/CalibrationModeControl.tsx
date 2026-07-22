import { useTranslation } from 'react-i18next';
import type { CalibrationMode } from '../../api/client';

const MODES_3: CalibrationMode[] = ['off', 'auto', 'on'];
const MODES_2: CalibrationMode[] = ['off', 'on'];

interface CalibrationModeControlProps {
  label: string;
  description?: string;
  value: CalibrationMode;
  onChange: (mode: CalibrationMode) => void;
  /** Render the 3-position (off/auto/on) control. When false, off/on only —
   *  the printer's firmware doesn't support the auto mode for this step. */
  autoSupported: boolean;
}

/**
 * Segmented off / auto / on control for a single print-calibration step.
 * Mirrors the preheat-override button group's styling. On a printer that
 * doesn't support the auto mode, only off/on render and a value of 'auto'
 * (saved from another printer) displays as 'on'.
 */
export function CalibrationModeControl({
  label,
  description,
  value,
  onChange,
  autoSupported,
}: CalibrationModeControlProps) {
  const { t } = useTranslation();
  const modes = autoSupported ? MODES_3 : MODES_2;
  // A saved 'auto' on a printer that can't do auto reads as 'on'.
  const effective: CalibrationMode = value === 'auto' && !autoSupported ? 'on' : value;

  return (
    <div>
      <div className="min-w-0 mb-1.5">
        <span className="text-sm text-white">{label}</span>
        {description ? <p className="text-xs text-bambu-gray">{description}</p> : null}
      </div>
      <div className="flex gap-1.5">
        {modes.map((mode) => (
          <button
            key={mode}
            type="button"
            onClick={() => onChange(mode)}
            className={`flex-1 px-2 py-1.5 text-xs rounded transition-colors ${
              effective === mode
                ? 'bg-bambu-green text-white'
                : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white'
            }`}
          >
            {t(`printModal.calibrationMode_${mode}`)}
          </button>
        ))}
      </div>
    </div>
  );
}
