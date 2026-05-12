import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useQuery } from '@tanstack/react-query';

import { api } from '../../api/client';
import type {
  CalibCapabilities,
  CalibFilamentIn,
  CaliMethod,
  CaliMode,
  NozzleVolumeType,
  PrinterStatus,
} from '../../api/client';

interface Props {
  printerId: number;
  caliMode: CaliMode;
  method: CaliMethod;
  capabilities: CalibCapabilities | undefined;
  onBack: () => void;
  onStart: (preset: {
    nozzle_diameter: number;
    nozzle_volume_type: NozzleVolumeType;
    extruder_id: number;
    filaments: CalibFilamentIn[];
  }) => Promise<void>;
}

interface LoadedSlot {
  ams_id: number;
  slot_id: number;
  tray_id: number;
  filament_id: string;
  filament_setting_id: string | null;
  label: string;
}

interface PerExtruderState {
  selectedSlot: LoadedSlot | null;
  bedTemp: number;
  nozzleTemp: number;
  maxVolSpeed: number;
}

const DEFAULT_PER_EXTRUDER: PerExtruderState = {
  selectedSlot: null,
  bedTemp: 60,
  nozzleTemp: 220,
  maxVolSpeed: 12,
};

export function CalibrationPresetPage({
  printerId,
  method,
  capabilities,
  onBack,
  onStart,
}: Props) {
  const { t } = useTranslation();

  const statusQuery = useQuery<PrinterStatus>({
    queryKey: ['printerStatus', printerId],
    queryFn: () => api.getPrinterStatus(printerId),
    refetchInterval: 5_000,
  });

  const firstNozzleDia = capabilities?.nozzles?.[0]?.diameter ?? 0.4;
  const [nozzleDia, setNozzleDia] = useState<number>(firstNozzleDia);
  const [nozzleVolType, setNozzleVolType] = useState<NozzleVolumeType>('standard');

  const isDual = Boolean(capabilities?.dual_extruder);
  const extruderList = useMemo(
    () => capabilities?.extruders ?? [{ id: 0, name: 'Main' }],
    [capabilities?.extruders],
  );
  const [activeExtruder, setActiveExtruder] = useState<number>(extruderList[0]?.id ?? 0);

  const [perExtruder, setPerExtruder] = useState<Record<number, PerExtruderState>>(() => {
    const init: Record<number, PerExtruderState> = {};
    for (const ex of extruderList) init[ex.id] = { ...DEFAULT_PER_EXTRUDER };
    return init;
  });

  const current = perExtruder[activeExtruder] ?? DEFAULT_PER_EXTRUDER;

  const patchCurrent = (p: Partial<PerExtruderState>) =>
    setPerExtruder((prev) => ({
      ...prev,
      [activeExtruder]: { ...(prev[activeExtruder] ?? DEFAULT_PER_EXTRUDER), ...p },
    }));

  const loadedSlots = useMemo<LoadedSlot[]>(() => {
    const units = statusQuery.data?.ams ?? [];
    const out: LoadedSlot[] = [];
    for (const unit of units) {
      for (const tray of unit.tray) {
        const loaded = tray.state === 11 || (tray.tray_info_idx && tray.tray_info_idx !== '');
        if (!loaded || !tray.tray_info_idx) continue;
        const globalTrayId = unit.id * 4 + tray.id;
        const label = `AMS ${unit.id + 1} · Slot ${tray.id + 1} · ${tray.tray_sub_brands ?? tray.tray_info_idx}`;
        out.push({
          ams_id: unit.id,
          slot_id: tray.id,
          tray_id: globalTrayId,
          filament_id: tray.tray_info_idx,
          filament_setting_id: null,
          label,
        });
      }
    }
    return out;
  }, [statusQuery.data]);

  const buildFilament = (st: PerExtruderState, exId: number): CalibFilamentIn | null => {
    if (!st.selectedSlot || st.bedTemp <= 0 || st.nozzleTemp <= 0 || st.maxVolSpeed <= 0) {
      return null;
    }
    return {
      ams_id: st.selectedSlot.ams_id,
      slot_id: st.selectedSlot.slot_id,
      tray_id: st.selectedSlot.tray_id,
      filament_id: st.selectedSlot.filament_id,
      filament_setting_id: st.selectedSlot.filament_setting_id,
      bed_temp: st.bedTemp,
      nozzle_temp: st.nozzleTemp,
      max_volumetric_speed: st.maxVolSpeed,
      extruder_id: isDual ? exId : undefined,
    };
  };

  const submit = async () => {
    if (method === 'auto' && isDual) {
      const filaments: CalibFilamentIn[] = [];
      for (const ex of extruderList) {
        const f = buildFilament(perExtruder[ex.id] ?? DEFAULT_PER_EXTRUDER, ex.id);
        if (f) filaments.push(f);
      }
      if (filaments.length === 0) return;
      await onStart({
        nozzle_diameter: nozzleDia,
        nozzle_volume_type: nozzleVolType,
        extruder_id: filaments[0].extruder_id ?? 0,
        filaments,
      });
      return;
    }

    const f = buildFilament(current, activeExtruder);
    if (!f) return;
    await onStart({
      nozzle_diameter: nozzleDia,
      nozzle_volume_type: nozzleVolType,
      extruder_id: activeExtruder,
      filaments: [f],
    });
  };

  const canStart = (() => {
    if (method === 'auto' && isDual) {
      return extruderList.some((ex) => buildFilament(perExtruder[ex.id] ?? DEFAULT_PER_EXTRUDER, ex.id) != null);
    }
    return buildFilament(current, activeExtruder) != null;
  })();

  const extruderLabel = (name: string) =>
    t(`filamentCali.extruder.${name.toLowerCase()}`, { defaultValue: name });

  return (
    <div className="space-y-4">
      <h3 className="text-base font-semibold text-white">{t('filamentCali.preset.heading')}</h3>

      {isDual && (
        <div className="flex gap-1 rounded-lg p-1 bg-bambu-dark border border-bambu-dark-tertiary">
          {extruderList.map((ex) => (
            <button
              key={ex.id}
              type="button"
              onClick={() => setActiveExtruder(ex.id)}
              className={`flex-1 px-3 py-1.5 text-sm rounded transition-colors ${
                activeExtruder === ex.id
                  ? 'bg-bambu-green text-white'
                  : 'text-bambu-gray hover:text-white'
              }`}
            >
              {extruderLabel(ex.name)}
            </button>
          ))}
        </div>
      )}

      <section>
        <div className="grid grid-cols-2 gap-2">
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleDia')}</span>
            <select
              value={nozzleDia}
              onChange={(e) => setNozzleDia(parseFloat(e.target.value))}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            >
              {(capabilities?.nozzles ?? [{ diameter: 0.4 }]).map((n, i) => (
                <option key={i} value={n.diameter ?? 0.4}>
                  {n.diameter ?? 0.4} mm
                </option>
              ))}
            </select>
          </label>
          <label className="block">
            <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleType')}</span>
            <select
              value={nozzleVolType}
              onChange={(e) => setNozzleVolType(e.target.value as NozzleVolumeType)}
              className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
            >
              <option value="standard">Standard</option>
              <option value="high_flow">High Flow</option>
              <option value="tpu_high_flow">TPU High Flow</option>
              <option value="hybrid">Hybrid</option>
            </select>
          </label>
        </div>
      </section>

      <section>
        <span className="text-sm font-medium text-bambu-gray block mb-2">
          {t('filamentCali.preset.selectFilament')}
        </span>
        {loadedSlots.length === 0 ? (
          <div className="p-3 bg-bambu-dark rounded text-sm text-bambu-gray">
            {t('filamentCali.preset.noLoadedSlot')}
          </div>
        ) : (
          <div className="space-y-2">
            {loadedSlots.map((s) => (
              <button
                key={`${s.ams_id}-${s.slot_id}`}
                type="button"
                onClick={() => patchCurrent({ selectedSlot: s })}
                className={`w-full text-left p-2 rounded border ${
                  current.selectedSlot?.ams_id === s.ams_id &&
                  current.selectedSlot.slot_id === s.slot_id
                    ? 'border-bambu-green bg-bambu-green/10'
                    : 'border-bambu-dark-tertiary bg-bambu-dark hover:border-bambu-green/50'
                }`}
              >
                <span className="text-white">{s.label}</span>
              </button>
            ))}
          </div>
        )}
      </section>

      <section className="grid grid-cols-3 gap-2">
        <label className="block">
          <span className="text-xs text-bambu-gray">{t('filamentCali.preset.bedTemp')}</span>
          <input
            type="number"
            value={current.bedTemp}
            onChange={(e) => patchCurrent({ bedTemp: parseInt(e.target.value, 10) || 0 })}
            className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
          />
        </label>
        <label className="block">
          <span className="text-xs text-bambu-gray">{t('filamentCali.preset.nozzleTemp')}</span>
          <input
            type="number"
            value={current.nozzleTemp}
            onChange={(e) => patchCurrent({ nozzleTemp: parseInt(e.target.value, 10) || 0 })}
            className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
          />
        </label>
        <label className="block">
          <span className="text-xs text-bambu-gray">{t('filamentCali.preset.maxVolSpeed')}</span>
          <input
            type="number"
            step="0.5"
            value={current.maxVolSpeed}
            onChange={(e) => patchCurrent({ maxVolSpeed: parseFloat(e.target.value) || 0 })}
            className="w-full bg-bambu-dark border border-bambu-dark-tertiary rounded px-2 py-1.5 text-white"
          />
        </label>
      </section>

      <div className="flex justify-between pt-2 border-t border-bambu-dark-tertiary">
        <button
          type="button"
          onClick={onBack}
          className="px-3 py-1.5 text-sm text-bambu-gray hover:text-white"
        >
          {t('filamentCali.back')}
        </button>
        <button
          type="button"
          onClick={submit}
          disabled={!canStart}
          className="px-4 py-2 rounded bg-bambu-green text-white text-sm font-medium disabled:opacity-40 disabled:cursor-not-allowed"
        >
          {t('filamentCali.startCalibration')}
        </button>
      </div>
    </div>
  );
}
