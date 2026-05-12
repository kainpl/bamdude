import { useEffect } from 'react';
import { useTranslation } from 'react-i18next';
import { X } from 'lucide-react';

import { useToast } from '../contexts/ToastContext';
import { useFilamentCalibration } from '../hooks/useFilamentCalibration';
import { CalibrationStartPage } from './calibration/CalibrationStartPage';
import { CalibrationPresetPage } from './calibration/CalibrationPresetPage';
import { CalibrationRunningPage } from './calibration/CalibrationRunningPage';
import { CalibrationManualSavePage } from './calibration/CalibrationManualSavePage';
import { CalibrationCoarseSavePage } from './calibration/CalibrationCoarseSavePage';
import { CalibrationFineSavePage } from './calibration/CalibrationFineSavePage';
import { CalibrationAutoSavePage } from './calibration/CalibrationAutoSavePage';
import { CalibrationTowerFinishPage } from './calibration/CalibrationTowerFinishPage';
import { CalibrationFinishPage } from './calibration/CalibrationFinishPage';
import { ResumeBanner } from './calibration/ResumeBanner';

interface Props {
  isOpen: boolean;
  onClose: () => void;
  printerId: number;
}

/**
 * Filament Calibration wizard shell. The hook owns the step machine and
 * data fetches; this component just renders the matching sub-page.
 */
export function FilamentCalibrationModal({ isOpen, onClose, printerId }: Props) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const cali = useFilamentCalibration(printerId, isOpen);

  useEffect(() => {
    if (cali.errorMsg) showToast(cali.errorMsg, 'error');
  }, [cali.errorMsg, showToast]);

  if (!isOpen) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60">
      <div className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-xl shadow-2xl w-full max-w-3xl mx-4 max-h-[90vh] overflow-y-auto">
        <div className="flex justify-between items-center p-4 border-b border-bambu-dark-tertiary">
          <h2 className="text-lg font-semibold text-white">{t('filamentCali.title')}</h2>
          <button
            onClick={onClose}
            aria-label="Close"
            className="p-1 text-bambu-gray hover:text-white rounded transition-colors"
          >
            <X className="h-5 w-5" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {cali.awaitingSession && cali.step === 'start' && (
            <ResumeBanner
              session={cali.awaitingSession}
              onResume={() => {
                cali.setSessionId(cali.awaitingSession!.id);
                cali.setStep('running');
              }}
              onDiscard={async () => {
                cali.setSessionId(cali.awaitingSession!.id);
                await cali.cancelSession();
              }}
            />
          )}

          {cali.step === 'start' && (
            <CalibrationStartPage
              capabilities={cali.capabilities}
              onPick={(mode, method) => {
                cali.setInput({ cali_mode: mode, method });
                cali.setStep('preset');
              }}
            />
          )}

          {cali.step === 'preset' && cali.input.cali_mode && (
            <CalibrationPresetPage
              printerId={printerId}
              caliMode={cali.input.cali_mode}
              method={cali.input.method ?? 'manual'}
              capabilities={cali.capabilities}
              onBack={() => cali.setStep('start')}
              onStart={async (preset) => {
                cali.setInput({
                  nozzle_diameter: preset.nozzle_diameter,
                  nozzle_volume_type: preset.nozzle_volume_type,
                  extruder_id: preset.extruder_id,
                  filaments: preset.filaments,
                });
                await cali.startSession({
                  cali_mode: cali.input.cali_mode!,
                  method: cali.input.method ?? 'manual',
                  nozzle_diameter: preset.nozzle_diameter,
                  nozzle_volume_type: preset.nozzle_volume_type,
                  extruder_id: preset.extruder_id,
                  filaments: preset.filaments,
                });
              }}
            />
          )}

          {cali.step === 'running' && cali.session && (
            <CalibrationRunningPage session={cali.session} onCancel={() => cali.cancelSession()} />
          )}

          {cali.step === 'manualSave' && cali.session && (
            <CalibrationManualSavePage
              session={cali.session}
              onSave={(body) => cali.submitManualResult(body)}
              onBack={() => cali.setStep('running')}
              isSubmitting={cali.isSubmitting}
            />
          )}

          {cali.step === 'coarseSave' && cali.session && (
            <CalibrationCoarseSavePage
              session={cali.session}
              onSubmit={(body) => cali.submitManualResult(body)}
              isSubmitting={cali.isSubmitting}
            />
          )}

          {cali.step === 'fineSave' && cali.session && (
            <CalibrationFineSavePage
              session={cali.session}
              onSubmit={(body) => cali.submitManualResult(body)}
              isSubmitting={cali.isSubmitting}
            />
          )}

          {cali.step === 'autoSave' && cali.session && (
            <CalibrationAutoSavePage
              session={cali.session}
              onSubmit={(body) => cali.submitAutoResult(body)}
              isSubmitting={cali.isSubmitting}
            />
          )}

          {cali.step === 'towerFinish' && cali.session && (
            <CalibrationTowerFinishPage
              session={cali.session}
              onClose={onClose}
              onCalibrateAnother={() => {
                cali.setSessionId(null);
                cali.setStep('start');
              }}
            />
          )}

          {cali.step === 'finish' && (
            <CalibrationFinishPage
              savedRows={cali.savedRows}
              onCalibrateAnother={() => {
                cali.setSessionId(null);
                cali.setStep('start');
              }}
              onClose={onClose}
            />
          )}
        </div>
      </div>
    </div>
  );
}
