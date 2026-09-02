import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Flame, Minus, Plus, Power, Square, Thermometer, X } from 'lucide-react';
import { api, ApiError, type PrinterStatus } from '../api/client';

/**
 * One window for the three heaters — nozzle (or both), bed, chamber.
 *
 * ⚠️ **The bounds are not written here.** They arrive in
 * `status.temperature_limits`, because the answer depends on the model, on what
 * the printer reported, and on the mains voltage: a 220 V X1 accepts a LOWER bed
 * temperature than a 110 V one. A table in the browser would be a second source
 * of truth that disagrees with the server the first time any of those changed —
 * and the disagreement would surface as a request the backend refuses for
 * reasons the UI cannot explain.
 *
 * ⚠️ **Zero is not "the bottom of the range", it is Off**, and it is exempt from
 * the range on purpose — BambuStudio does the same with `TempInput::AddTemp(0)`.
 * That is why the Off button is not simply the minus key held down: stepping
 * would stop at the floor, which on a bed is 20 °C, and leave the heater on.
 *
 * ⚠️ **No mid-print guard here**, unlike the fan dialog. Adjusting a temperature
 * while a print runs is ordinary tuning and BambuStudio gates none of the three
 * on print state; a confirmation copied from the fans would obstruct the normal
 * use of this window.
 */

// Nothing in the protocol quantises a setpoint, so the step is purely a
// convenience for the arrows. Five degrees is small enough to trim a first
// layer and large enough to be worth a button.
const STEP = 5;

type Part = 'nozzle' | 'bed' | 'chamber';

interface Row {
  key: string;
  part: Part;
  extruderIndex: number;
  label: string;
  tint: string;
  current?: number;
  target?: number;
  limits: [number, number];
  disabledReason?: string;
}

interface Props {
  printerId: number;
  isOpen: boolean;
  onClose: () => void;
  status: PrinterStatus;
  isDualNozzle: boolean;
  supportsChamberHeater: boolean;
  canControl: boolean;
}

export function TemperatureModal({
  printerId,
  isOpen,
  onClose,
  status,
  isDualNozzle,
  supportsChamberHeater,
  canControl,
}: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  // What the user is typing, per row. Absent means "show the printer's value" —
  // an empty string is a real edit-in-progress and must not collapse to that.
  const [draft, setDraft] = useState<Record<string, string>>({});

  useEffect(() => {
    if (!isOpen) {
      setDraft({});
      setError(null);
    }
  }, [isOpen]);

  const mutation = useMutation({
    mutationFn: ({ part, target, extruderIndex }: { part: Part; target: number; extruderIndex: number }) =>
      api.setTemperature(printerId, part, target, extruderIndex),
    onSuccess: () => {
      setError(null);
      queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });
    },
    onError: (e: ApiError) => setError(e.message),
  });

  if (!isOpen) return null;

  const temps = status.temperatures ?? {};
  const limits = status.temperature_limits ?? {};
  const nozzleLimits = limits.nozzle ?? [20, 300];
  const bedLimits = limits.bed ?? [20, 120];
  const chamberLimits = limits.chamber ?? [0, 60];

  // ⚠️ A MISSING entry means the printer cannot detect a hotend at all (the A
  // and P series), which is not the same as "no hotend fitted". Only an explicit
  // false may disable the row — treating absent as false would grey out the
  // control on most of the fleet.
  const noHotend = (index: number) => status.ext_has_nozzle?.[index] === false;

  const rows: Row[] = [];
  if (isDualNozzle) {
    rows.push({
      key: 'nozzle-0',
      part: 'nozzle',
      extruderIndex: 0,
      label: t('printers.temperatureControl.nozzleRight'),
      tint: 'text-orange-400',
      current: temps.nozzle,
      target: temps.nozzle_target,
      limits: nozzleLimits,
      disabledReason: noHotend(0) ? t('printers.temperatureControl.noHotend') : undefined,
    });
    rows.push({
      key: 'nozzle-1',
      part: 'nozzle',
      extruderIndex: 1,
      label: t('printers.temperatureControl.nozzleLeft'),
      tint: 'text-orange-400',
      current: temps.nozzle_2,
      target: temps.nozzle_2_target,
      limits: nozzleLimits,
      disabledReason: noHotend(1) ? t('printers.temperatureControl.noHotend') : undefined,
    });
  } else {
    rows.push({
      key: 'nozzle-0',
      part: 'nozzle',
      extruderIndex: 0,
      label: t('printers.temperatures.nozzle'),
      tint: 'text-orange-400',
      current: temps.nozzle,
      target: temps.nozzle_target,
      limits: nozzleLimits,
      disabledReason: noHotend(0) ? t('printers.temperatureControl.noHotend') : undefined,
    });
  }
  rows.push({
    key: 'bed',
    part: 'bed',
    extruderIndex: 0,
    label: t('printers.temperatures.bed'),
    tint: 'text-blue-400',
    current: temps.bed,
    target: temps.bed_target,
    limits: bedLimits,
  });
  if (temps.chamber !== undefined) {
    rows.push({
      key: 'chamber',
      part: 'chamber',
      extruderIndex: 0,
      label: t('printers.temperatures.chamber'),
      tint: 'text-green-400',
      current: temps.chamber,
      target: temps.chamber_target,
      limits: chamberLimits,
      // A sensor is not a heater: the X1C and the P2S report a chamber
      // temperature they have no way of changing.
      disabledReason: supportsChamberHeater ? undefined : t('printers.temperatureControl.sensorOnly'),
    });
  }

  const shown = (r: Row) => draft[r.key] ?? String(Math.round(r.target ?? 0));

  const send = (r: Row, value: number) => {
    setDraft((d) => {
      const next = { ...d };
      delete next[r.key];
      return next;
    });
    mutation.mutate({ part: r.part, target: value, extruderIndex: r.extruderIndex });
  };

  const step = (r: Row, delta: number) => {
    const from = Number(shown(r)) || 0;
    // Stepping up from Off lands on the floor rather than on 5 °C, which is the
    // first value the machine would actually accept.
    const raw = from === 0 && delta > 0 ? r.limits[0] : from + delta * STEP;
    send(r, Math.max(0, Math.min(r.limits[1], raw)));
  };

  const commit = (r: Row) => {
    const typed = draft[r.key];
    if (typed === undefined) return;
    const value = Number(typed);
    if (!Number.isFinite(value) || typed.trim() === '') {
      setDraft((d) => {
        const next = { ...d };
        delete next[r.key];
        return next;
      });
      return;
    }
    send(r, Math.max(0, Math.min(r.limits[1], Math.round(value))));
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white">{t('printers.temperatureControl.title')}</h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-1.5">
          {rows.map((r) => {
            const adjustable = canControl && !r.disabledReason;
            const target = r.target ?? 0;
            return (
              <div key={r.key} className="px-2 py-2 rounded bg-bambu-dark">
                <div className="flex items-center gap-2">
                  {target > 0 ? (
                    <Flame className={`w-4 h-4 shrink-0 ${r.tint}`} />
                  ) : (
                    <Thermometer className="w-4 h-4 shrink-0 text-bambu-gray/50" />
                  )}
                  <span className="text-xs text-white truncate flex-1 min-w-0">{r.label}</span>
                  <span className="text-[11px] text-bambu-gray tabular-nums shrink-0">
                    {Math.round(r.current ?? 0)}°C
                  </span>
                </div>

                {adjustable ? (
                  <div className="flex items-center gap-1 mt-1.5">
                    <button
                      onClick={() => step(r, -1)}
                      disabled={Number(shown(r)) <= 0 || mutation.isPending}
                      className="w-6 h-6 rounded bg-bambu-dark-tertiary text-bambu-gray hover:text-white disabled:opacity-40 flex items-center justify-center transition-colors"
                      aria-label={t('printers.temperatureControl.decrease')}
                    >
                      <Minus className="w-3 h-3" />
                    </button>
                    <input
                      type="number"
                      value={shown(r)}
                      onChange={(e) => setDraft((d) => ({ ...d, [r.key]: e.target.value }))}
                      onBlur={() => commit(r)}
                      onKeyDown={(e) => {
                        if (e.key === 'Enter') commit(r);
                      }}
                      className="w-16 px-1.5 py-1 rounded bg-bambu-dark-tertiary text-white text-[11px] text-center tabular-nums border border-transparent focus:border-bambu-green focus:outline-none"
                    />
                    <button
                      onClick={() => step(r, 1)}
                      disabled={Number(shown(r)) >= r.limits[1] || mutation.isPending}
                      className="w-6 h-6 rounded bg-bambu-dark-tertiary text-bambu-gray hover:text-white disabled:opacity-40 flex items-center justify-center transition-colors"
                      aria-label={t('printers.temperatureControl.increase')}
                    >
                      <Plus className="w-3 h-3" />
                    </button>
                    <span className="text-[10px] text-bambu-gray/70 tabular-nums ml-1">
                      {r.limits[0]}–{r.limits[1]}°C
                    </span>
                    <button
                      onClick={() => send(r, 0)}
                      disabled={target <= 0 || mutation.isPending}
                      className="ml-auto px-2 h-6 rounded bg-bambu-dark-tertiary text-[10px] text-bambu-gray hover:text-white disabled:opacity-40 flex items-center gap-1 transition-colors"
                    >
                      <Power className="w-3 h-3" />
                      {t('printers.temperatureControl.turnOff')}
                    </button>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5 mt-1.5 text-[11px] text-bambu-gray">
                    <Square className="w-3 h-3" />
                    {r.disabledReason ?? t('printers.temperatureControl.noPermission')}
                  </div>
                )}
              </div>
            );
          })}

          {error && <p className="text-[11px] text-red-400 leading-relaxed">{error}</p>}
        </div>
      </div>
    </div>
  );
}
