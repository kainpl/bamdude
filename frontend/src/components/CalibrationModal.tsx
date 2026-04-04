import { useState } from 'react';
import { useMutation } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Play } from 'lucide-react';
import { api } from '../api/client';
import { Button } from './Button';
import { Toggle } from './Toggle';

interface CalibrationModalProps {
  printerId: number;
  printerName: string;
  printerModel: string | null;
  onClose: () => void;
}

// Which calibrations are available per model
function getAvailableCalibrations(model: string | null): {
  bed_leveling: boolean;
  vibration: boolean;
  motor_noise: boolean;
  nozzle_offset: boolean;
  high_temp_heatbed: boolean;
} {
  const m = (model || '').toUpperCase();
  const isH2D = m.includes('H2D');
  const isH2 = m.startsWith('H2');
  const isX1E = m === 'X1E';
  const isP2S = m === 'P2S';

  return {
    bed_leveling: true,
    vibration: !isP2S,
    motor_noise: true,
    nozzle_offset: isH2D,
    high_temp_heatbed: isH2 || isX1E,
  };
}

export function CalibrationModal({ printerId, printerName, printerModel, onClose }: CalibrationModalProps) {
  const { t } = useTranslation();
  const available = getAvailableCalibrations(printerModel);

  const [bedLeveling, setBedLeveling] = useState(false);
  const [vibration, setVibration] = useState(false);
  const [motorNoise, setMotorNoise] = useState(false);
  const [nozzleOffset, setNozzleOffset] = useState(false);
  const [highTempHeatbed, setHighTempHeatbed] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasSelection = bedLeveling || vibration || motorNoise || nozzleOffset || highTempHeatbed;

  const mutation = useMutation({
    mutationFn: () => api.startCalibration(printerId, {
      bed_leveling: bedLeveling,
      vibration: vibration,
      motor_noise: motorNoise,
      nozzle_offset: nozzleOffset,
      high_temp_heatbed: highTempHeatbed,
    }),
    onSuccess: () => onClose(),
    onError: (err: Error) => setError(err.message),
  });

  const calibrations = [
    { key: 'bed_leveling', label: t('printers.calibration.bedLeveling'), desc: t('printers.calibration.bedLevelingDesc'), available: available.bed_leveling, checked: bedLeveling, onChange: setBedLeveling },
    { key: 'vibration', label: t('printers.calibration.vibration'), desc: t('printers.calibration.vibrationDesc'), available: available.vibration, checked: vibration, onChange: setVibration },
    { key: 'motor_noise', label: t('printers.calibration.motorNoise'), desc: t('printers.calibration.motorNoiseDesc'), available: available.motor_noise, checked: motorNoise, onChange: setMotorNoise },
    { key: 'nozzle_offset', label: t('printers.calibration.nozzleOffset'), desc: t('printers.calibration.nozzleOffsetDesc'), available: available.nozzle_offset, checked: nozzleOffset, onChange: setNozzleOffset },
    { key: 'high_temp_heatbed', label: t('printers.calibration.highTempHeatbed'), desc: t('printers.calibration.highTempHeatbedDesc'), available: available.high_temp_heatbed, checked: highTempHeatbed, onChange: setHighTempHeatbed },
  ];

  const availableCalibrations = calibrations.filter(c => c.available);

  return (
    <div className="fixed inset-0 bg-black/70 flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-bambu-dark-tertiary">
          <div>
            <h2 className="text-lg font-semibold text-white">{t('printers.calibration.title')}</h2>
            <p className="text-sm text-bambu-gray">{printerName} ({printerModel || '?'})</p>
          </div>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="px-5 py-4 space-y-3">
          {error && (
            <div className="p-3 bg-red-500/20 border border-red-500/50 rounded text-red-400 text-sm">
              {error}
            </div>
          )}

          {availableCalibrations.map((cal) => (
            <div key={cal.key} className="flex items-center justify-between">
              <div>
                <p className="text-sm text-white">{cal.label}</p>
                <p className="text-xs text-bambu-gray">{cal.desc}</p>
              </div>
              <Toggle
                checked={cal.checked}
                onChange={cal.onChange}
              />
            </div>
          ))}
        </div>

        {/* Footer */}
        <div className="flex gap-3 px-5 py-4 border-t border-bambu-dark-tertiary">
          <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
            {t('common.cancel')}
          </Button>
          <Button
            onClick={() => mutation.mutate()}
            disabled={!hasSelection || mutation.isPending}
            className="flex-1"
          >
            {mutation.isPending ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Play className="w-4 h-4" />
            )}
            {t('printers.calibration.start')}
          </Button>
        </div>
      </div>
    </div>
  );
}
