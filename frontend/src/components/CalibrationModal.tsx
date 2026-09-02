import { useEffect, useRef, useState } from 'react';
import { useMutation, useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { X, Loader2, Play, CheckCircle2, Circle } from 'lucide-react';
import { api } from '../api/client';
import { Button } from './Button';
import { Toggle } from './Toggle';

interface CalibrationModalProps {
  printerId: number;
  printerName: string;
  printerModel: string | null;
  onClose: () => void;
}

// Availability of the seven device calibrations for a printer's model+firmware.
// Resolved SERVER-SIDE (mirrored BambuStudio config base + the printer's live
// support_* MQTT flags) via GET /printers/{id}/calibration/options — no ad-hoc
// model-string gating in the client.
type CalibrationOptions = {
  lidar: boolean;
  bed_leveling: boolean;
  vibration: boolean;
  motor_noise: boolean;
  nozzle_offset: boolean;
  high_temp_heatbed: boolean;
  clump_pos: boolean;
};

export function CalibrationModal({ printerId, printerName, printerModel, onClose }: CalibrationModalProps) {
  const { t } = useTranslation();
  const [phase, setPhase] = useState<'select' | 'running'>('select');

  const { data: available, isLoading: optionsLoading } = useQuery<CalibrationOptions>({
    queryKey: ['calibration-options', printerId],
    queryFn: () => api.getCalibrationOptions(printerId),
    enabled: phase === 'select',
  });

  const [lidar, setLidar] = useState(false);
  const [bedLeveling, setBedLeveling] = useState(false);
  const [vibration, setVibration] = useState(false);
  const [motorNoise, setMotorNoise] = useState(false);
  const [nozzleOffset, setNozzleOffset] = useState(false);
  const [highTempHeatbed, setHighTempHeatbed] = useState(false);
  const [clumpPos, setClumpPos] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const hasSelection =
    lidar || bedLeveling || vibration || motorNoise || nozzleOffset || highTempHeatbed || clumpPos;

  const mutation = useMutation({
    mutationFn: () => api.startCalibration(printerId, {
      bed_leveling: bedLeveling,
      vibration: vibration,
      motor_noise: motorNoise,
      nozzle_offset: nozzleOffset,
      high_temp_heatbed: highTempHeatbed,
      lidar: lidar,
      clump_pos: clumpPos,
    }),
    onSuccess: () => setPhase('running'),
    onError: (err: Error) => setError(err.message),
  });

  // Live calibration-stage flow. The printer reports its stage sequence (`stg`)
  // and the currently-running stage (`stg_cur`) via MQTT status; poll it while
  // running. `stg_cur === -1` means "not calibrating" — either not begun yet or
  // finished, disambiguated by `sawActive`.
  const { data: status } = useQuery({
    queryKey: ['printer-status-cali', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    enabled: phase === 'running',
    refetchInterval: phase === 'running' ? 1500 : false,
  });
  const stg = status?.stg ?? [];
  const stgNames = status?.stg_names ?? [];
  const stgCur = status?.stg_cur ?? -1;
  const sawActive = useRef(false);
  useEffect(() => {
    if (phase === 'running' && stgCur >= 0) sawActive.current = true;
  }, [phase, stgCur]);
  const done = phase === 'running' && sawActive.current && stgCur === -1;
  const curIndex = stg.indexOf(stgCur);

  const calibrations = [
    { key: 'lidar', label: t('printers.calibration.lidar'), desc: t('printers.calibration.lidarDesc'), available: available?.lidar ?? false, checked: lidar, onChange: setLidar },
    { key: 'bed_leveling', label: t('printers.calibration.bedLeveling'), desc: t('printers.calibration.bedLevelingDesc'), available: available?.bed_leveling ?? false, checked: bedLeveling, onChange: setBedLeveling },
    { key: 'vibration', label: t('printers.calibration.vibration'), desc: t('printers.calibration.vibrationDesc'), available: available?.vibration ?? false, checked: vibration, onChange: setVibration },
    { key: 'motor_noise', label: t('printers.calibration.motorNoise'), desc: t('printers.calibration.motorNoiseDesc'), available: available?.motor_noise ?? false, checked: motorNoise, onChange: setMotorNoise },
    { key: 'nozzle_offset', label: t('printers.calibration.nozzleOffset'), desc: t('printers.calibration.nozzleOffsetDesc'), available: available?.nozzle_offset ?? false, checked: nozzleOffset, onChange: setNozzleOffset },
    { key: 'high_temp_heatbed', label: t('printers.calibration.highTempHeatbed'), desc: t('printers.calibration.highTempHeatbedDesc'), available: available?.high_temp_heatbed ?? false, checked: highTempHeatbed, onChange: setHighTempHeatbed },
    { key: 'clump_pos', label: t('printers.calibration.clumpPos'), desc: t('printers.calibration.clumpPosDesc'), available: available?.clump_pos ?? false, checked: clumpPos, onChange: setClumpPos },
  ];

  const availableCalibrations = calibrations.filter(c => c.available);

  return (
    <div className="fixed inset-0 bg-black/50 backdrop-blur-sm flex items-center justify-center z-50 p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary rounded-xl border border-bambu-dark-tertiary w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex items-center justify-between px-5 py-4 border-b border-bambu-dark-tertiary">
          <div>
            <h2 className="text-lg font-semibold text-white">
              {phase === 'running' ? t('printers.calibration.runningTitle') : t('printers.calibration.title')}
            </h2>
            <p className="text-sm text-bambu-gray">{printerName} ({printerModel || '?'})</p>
          </div>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors">
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Content */}
        <div className="px-5 py-4 space-y-3">
          {error && (
            <div className="p-3 bg-red-100 dark:bg-red-500/20 border border-red-300 dark:border-red-500/50 rounded text-red-700 dark:text-red-400 text-sm">
              {error}
            </div>
          )}

          {phase === 'running' ? (
            stg.length === 0 && !done ? (
              <div className="flex items-center gap-2 py-4 text-sm text-bambu-gray">
                <Loader2 className="w-4 h-4 animate-spin" /> {t('printers.calibration.starting')}
              </div>
            ) : (
              <div className="space-y-2">
                {stg.map((s, i) => {
                  const state = done || i < curIndex ? 'done' : i === curIndex ? 'active' : 'pending';
                  return (
                    <div key={`${s}-${i}`} className="flex items-center gap-2">
                      {state === 'done' ? (
                        <CheckCircle2 className="w-4 h-4 text-bambu-green flex-shrink-0" />
                      ) : state === 'active' ? (
                        <Loader2 className="w-4 h-4 animate-spin text-bambu-green flex-shrink-0" />
                      ) : (
                        <Circle className="w-4 h-4 text-bambu-dark-tertiary flex-shrink-0" />
                      )}
                      <span className={`text-sm ${state === 'pending' ? 'text-bambu-gray' : 'text-white'}`}>
                        {stgNames[i] || `${t('printers.calibration.stage')} ${s}`}
                      </span>
                    </div>
                  );
                })}
                {done && (
                  <p className="text-sm text-bambu-green pt-1">{t('printers.calibration.complete')}</p>
                )}
              </div>
            )
          ) : optionsLoading ? (
            <div className="flex justify-center py-6">
              <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
            </div>
          ) : availableCalibrations.length === 0 ? (
            <p className="text-sm text-bambu-gray text-center py-4">
              {t('printers.calibration.noneAvailable')}
            </p>
          ) : (
            availableCalibrations.map((cal) => (
              <div key={cal.key} className="flex items-center justify-between">
                <div>
                  <p className="text-sm text-white">{cal.label}</p>
                  <p className="text-xs text-bambu-gray">{cal.desc}</p>
                </div>
                <Toggle checked={cal.checked} onChange={cal.onChange} />
              </div>
            ))
          )}
        </div>

        {/* Footer */}
        <div className="flex gap-3 px-5 py-4 border-t border-bambu-dark-tertiary">
          {phase === 'running' ? (
            <Button type="button" variant="secondary" onClick={onClose} className="flex-1">
              {done ? t('printers.calibration.close') : t('common.cancel')}
            </Button>
          ) : (
            <>
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
            </>
          )}
        </div>
      </div>
    </div>
  );
}
