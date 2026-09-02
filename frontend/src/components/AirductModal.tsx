import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { AirVent, AlertTriangle, Fan, Minus, Plus, Wind, X } from 'lucide-react';
import { api, ApiError, type AirductFan } from '../api/client';

/**
 * One window for the whole air duct: the mode, its "Filter" sub-mode, and every
 * fan the printer reports.
 *
 * ⚠️ **This exists because the sub-mode has nowhere else to live.** Speed
 * belongs to one fan and fits on its badge; the mode governs all of them, and
 * filtration governs the mode. Hanging either off a single fan's menu would put
 * a machine-wide setting behind whichever fan someone happened to click.
 *
 * The card keeps its badges as the readout. This is the control surface — the
 * same division BambuStudio makes between its status panel and its fan popup.
 *
 * ⚠️ **Three actions, three different rules mid-print, and they are not
 * interchangeable** (BS ``FanControlPopupNew``):
 *
 * - the **mode** cannot be changed at all while printing — BS shows an OK-only
 *   dialog and does not publish. So the buttons are disabled and say why,
 *   rather than asking a question whose only answer is "no";
 * - turning **filtration on** is a warning with "change anyway" — it costs
 *   cooling rather than contradicting the material;
 * - turning filtration **off**, and changing a **speed**, ask nothing extra
 *   here beyond the speed warning the card already carries.
 */

const FAN_LOOKS: Record<number, { Icon: typeof Wind; tint: string }> = {
  1: { Icon: Fan, tint: 'text-cyan-400' },
  3: { Icon: AirVent, tint: 'text-green-400' },
};
const FAN_LOOK_DEFAULT = { Icon: Wind, tint: 'text-blue-400' };

// BambuStudio counts gears 1..10 and sends gear x 10; a step is therefore ten
// percent, and there is nothing in between for the printer to receive.
const GEAR = 10;

interface Props {
  printerId: number;
  isOpen: boolean;
  onClose: () => void;
  fans: AirductFan[];
  modes: number[];
  currentMode: number;
  subMode: number;
  supportsCoolingFilter: boolean;
  isPrinting: boolean;
  canControl: boolean;
}

export function AirductModal({
  printerId,
  isOpen,
  onClose,
  fans,
  modes,
  currentMode,
  subMode,
  supportsCoolingFilter,
  isPrinting,
  canControl,
}: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [confirmFilter, setConfirmFilter] = useState(false);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });

  // Named and ordered here rather than passed in: the card computes these
  // inside its own render callback, and reaching for them from out here is what
  // makes a component depend on where it happens to be mounted.
  //
  // ⚠️ The name comes from the backend, which resolves it per model AND per
  // mode — part 10 is the left auxiliary on a P2S and the right one on an X2D.
  // Choosing a label by id here would be wrong on half the fleet.
  const fanName = (f: AirductFan) =>
    f.label_key
      ? t(`printers.fans.${f.label_key}`, f.label ?? '')
      : (f.label ??
        (f.part_id === 1
          ? t('printers.fans.partCooling')
          : f.part_id === 3
            ? t('printers.fans.chamber')
            : t('printers.fans.auxiliary')));

  // Part cooling first, then left before right — the sides are mirrored between
  // the P2S and the X2D, so part-id order would read backwards on one of them.
  const ordered = [...fans].sort((a, b) => {
    if ((a.part_id === 1) !== (b.part_id === 1)) return a.part_id === 1 ? -1 : 1;
    const side = (f: AirductFan) => (/left/i.test(f.label ?? '') ? 0 : /right/i.test(f.label ?? '') ? 1 : 2);
    return side(a) - side(b) || a.part_id - b.part_id;
  });

  const modeMutation = useMutation({
    mutationFn: ({ modeId, submode, confirm }: { modeId: number; submode?: number; confirm?: boolean }) =>
      api.setAirductMode(printerId, modeId, submode ?? -1, confirm ?? false),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const speedMutation = useMutation({
    mutationFn: ({ partId, percent }: { partId: number; percent: number }) =>
      api.setFanSpeed(printerId, partId, percent, isPrinting),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  if (!isOpen) return null;

  const filterOn = subMode === 1;
  // BS shows the toggle only on the cooling mode, and only where the hardware
  // has it — the two questions are separate and both must pass.
  const showFilter = supportsCoolingFilter && currentMode === 0;

  const step = (f: AirductFan, delta: number) => {
    // Clamp to the range the part declared, then to the gears. Stepping below
    // the lowest gear is how BambuStudio turns a fan off, so 0 stays reachable.
    const next = Math.max(0, Math.min(100, Math.round((f.speed + delta * GEAR) / GEAR) * GEAR));
    const bounded = next === 0 ? 0 : Math.max(f.range_start, Math.min(f.range_end, next));
    if (bounded !== f.speed) speedMutation.mutate({ partId: f.part_id, percent: bounded });
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl w-full max-w-md"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white">{t('printers.airduct.title')}</h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {modes.length > 0 && (
            <div>
              <div className="text-[10px] uppercase tracking-wider text-bambu-gray mb-2">
                {t('printers.airduct.mode')}
              </div>
              <div className="flex flex-wrap gap-2">
                {modes.map((m) => (
                  <button
                    key={m}
                    // Also disabled for the mode already active: re-selecting
                    // it is a no-op the user cannot tell from a real change, and
                    // it was the click that could be repeated while the printer
                    // was still confirming.
                    disabled={!canControl || isPrinting || modeMutation.isPending || m === currentMode}
                    onClick={() => modeMutation.mutate({ modeId: m })}
                    className={`px-3 py-1.5 rounded-lg text-xs transition-colors ${
                      m === currentMode
                        ? 'bg-bambu-green/20 text-bambu-green'
                        : 'bg-bambu-dark text-bambu-gray hover:bg-bambu-dark-tertiary disabled:hover:bg-bambu-dark'
                    } ${!canControl || isPrinting ? 'opacity-50 cursor-not-allowed' : ''} ${
                      m === currentMode ? 'cursor-default' : ''
                    }`}
                  >
                    {t(`printers.airduct.modes.${m}`, `Mode ${m}`)}
                  </button>
                ))}
              </div>
              {isPrinting && (
                <p className="mt-2 text-[10px] text-bambu-gray leading-relaxed">
                  {t('printers.airduct.modeLockedWhilePrinting')}
                </p>
              )}
            </div>
          )}

          {showFilter && (
            <div className="flex items-start justify-between gap-3 pt-1">
              <div className="min-w-0">
                <div className="text-xs text-white">{t('printers.airduct.filter')}</div>
                <p className="text-[10px] text-bambu-gray leading-relaxed">{t('printers.airduct.filterHint')}</p>
              </div>
              <button
                disabled={!canControl || modeMutation.isPending}
                onClick={() => {
                  // Only switching it ON is warned about; off gives cooling back.
                  if (!filterOn && isPrinting && !confirmFilter) {
                    setConfirmFilter(true);
                    return;
                  }
                  setConfirmFilter(false);
                  modeMutation.mutate({ modeId: currentMode, submode: filterOn ? 0 : 1, confirm: true });
                }}
                className={`shrink-0 w-11 h-6 rounded-full transition-colors relative ${
                  filterOn ? 'bg-bambu-green/60' : 'bg-bambu-dark-tertiary'
                } ${!canControl ? 'opacity-50 cursor-not-allowed' : ''}`}
                aria-pressed={filterOn}
              >
                <span
                  className={`absolute top-0.5 w-5 h-5 rounded-full bg-white transition-all ${
                    filterOn ? 'left-[22px]' : 'left-0.5'
                  }`}
                />
              </button>
            </div>
          )}

          {confirmFilter && (
            <div className="rounded-lg border border-yellow-500/40 bg-yellow-500/10 p-3">
              <div className="flex items-start gap-2 mb-2">
                <AlertTriangle className="w-4 h-4 text-yellow-400 shrink-0 mt-0.5" />
                <p className="text-[11px] text-bambu-gray leading-relaxed">
                  {t('printers.airduct.filterPrintingWarning')}
                </p>
              </div>
              <div className="flex gap-2">
                <button
                  onClick={() => {
                    setConfirmFilter(false);
                    modeMutation.mutate({ modeId: currentMode, submode: 1, confirm: true });
                  }}
                  className="px-3 py-1.5 rounded-lg text-[11px] bg-bambu-green/20 text-bambu-green hover:bg-bambu-green/30 transition-colors"
                >
                  {t('printers.fans.changeAnyway')}
                </button>
                <button
                  onClick={() => setConfirmFilter(false)}
                  className="px-3 py-1.5 rounded-lg text-[11px] bg-bambu-dark text-bambu-gray hover:bg-bambu-dark-tertiary transition-colors"
                >
                  {t('common.cancel')}
                </button>
              </div>
            </div>
          )}

          <div>
            <div className="text-[10px] uppercase tracking-wider text-bambu-gray mb-2">
              {t('printers.airduct.fans')}
            </div>
            <div className="space-y-1.5">
              {ordered.map((f) => {
                const { Icon, tint } = FAN_LOOKS[f.part_id] ?? FAN_LOOK_DEFAULT;
                const control = f.control ?? (f.controllable ? 'ctrl' : 'off');
                const adjustable = canControl && control === 'ctrl';
                return (
                  <div key={f.part_id} className="flex items-center gap-2 px-2 py-1.5 rounded bg-bambu-dark">
                    <Icon className={`w-4 h-4 shrink-0 ${f.speed > 0 ? tint : 'text-bambu-gray/50'}`} />
                    <span className="text-xs text-white truncate flex-1 min-w-0">{fanName(f)}</span>
                    {adjustable ? (
                      <div className="flex items-center gap-1 shrink-0">
                        <button
                          onClick={() => step(f, -1)}
                          disabled={f.speed <= 0 || speedMutation.isPending}
                          className="w-6 h-6 rounded bg-bambu-dark-tertiary text-bambu-gray hover:text-white disabled:opacity-40 flex items-center justify-center transition-colors"
                        >
                          <Minus className="w-3 h-3" />
                        </button>
                        <span className="text-[11px] text-white w-10 text-center tabular-nums">{f.speed}%</span>
                        <button
                          onClick={() => step(f, 1)}
                          disabled={f.speed >= 100 || speedMutation.isPending}
                          className="w-6 h-6 rounded bg-bambu-dark-tertiary text-bambu-gray hover:text-white disabled:opacity-40 flex items-center justify-center transition-colors"
                        >
                          <Plus className="w-3 h-3" />
                        </button>
                      </div>
                    ) : (
                      // BambuStudio writes the word where the control would be —
                      // "Off" and "Auto" are different situations and neither is
                      // an adjustable zero.
                      <span className="text-[11px] text-bambu-gray shrink-0">
                        {control === 'off' ? t('printers.airduct.fanOff') : t('printers.airduct.fanAuto')}
                      </span>
                    )}
                  </div>
                );
              })}
            </div>
          </div>

          {error && <p className="text-[11px] text-red-400 leading-relaxed">{error}</p>}
        </div>
      </div>
    </div>
  );
}
