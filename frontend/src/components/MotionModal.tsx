import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { ArrowDown, ArrowDownToLine, ArrowUp, ArrowUpFromLine, Unlock, X } from 'lucide-react';
import { api, ApiError, type PrinterStatus } from '../api/client';
import { AxisJoystick } from './AxisJoystick';
import { ExtruderGraphic } from './ExtruderGraphic';

/**
 * Moving the head, the bed and the extruder by hand — BambuStudio's axis panel.
 *
 * ⚠️ **Fixed 1 mm / 10 mm steps, not a free distance field, and that is a
 * protocol fact rather than a design preference.** On machines that speak the
 * newer wire protocol the backend sends `xyz_ctrl`, which carries a direction
 * and a coarse/fine flag and nothing else — 3 mm and 9 mm arrive identical. An
 * input box here would promise a precision the machine never receives.
 *
 * ⚠️ **The direction each button sends is BambuStudio's, unflipped.** Whether Y
 * and Z need inverting depends on the printer's frame (a bed-slinger's Z carries
 * the toolhead, not the bed) and the backend applies that — so the same button
 * sends the same number for every model, and "up" looks like up everywhere. Do
 * not "fix" the sign here; that flip is what upstream #1334 was.
 *
 * ⚠️ **Nothing moves until the printer is homed, and only half of that is
 * BambuStudio's rule.** Studio checks the home flags before an X/Y move and
 * returns without publishing — so refusing X and Y is parity. For Z it does the
 * opposite: it sends the move and only then advises recentering. Refusing Z is
 * therefore ours, matching the card's bed-jog control, whose "move anyway"
 * escape hatch was removed upstream (#2579) because it drove the move with the
 * soft endstops disabled. Leaving Z open in this window would reopen that door
 * beside a control that closed it.
 *
 * The homed state is the printer's own (`home_flag` bits 0/1/2), not a
 * per-browser-session guess — the guess was wrong in both directions.
 */

// The two step sizes BambuStudio offers, and the only two the newer protocol can
// tell apart (its cutoff is exactly 10).
const STEP_FINE = 1;
const STEP_COARSE = 10;

interface Props {
  printerId: number;
  isOpen: boolean;
  onClose: () => void;
  status: PrinterStatus;
  isDualNozzle: boolean;
  canControl: boolean;
}

export function MotionModal({ printerId, isOpen, onClose, status, isDualNozzle, canControl }: Props) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);
  const [extruderIndex, setExtruderIndex] = useState(0);

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ['printerStatus', printerId] });

  const jog = useMutation({
    mutationFn: ({ axis, distance }: { axis: 'x' | 'y' | 'z' | 'e'; distance: number }) =>
      api.jogAxis(printerId, axis, distance, extruderIndex),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const home = useMutation({
    mutationFn: () => api.homeAxes(printerId, 'all'),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  const release = useMutation({
    mutationFn: () => api.disableSteppers(printerId),
    onSuccess: () => {
      setError(null);
      invalidate();
    },
    onError: (e: ApiError) => setError(e.message),
  });

  if (!isOpen) return null;

  const busy = jog.isPending || home.isPending || release.isPending;
  const printing = status.state === 'RUNNING' || status.state === 'PAUSE';

  // ⚠️ Absent means the printer never reported it, which reads as homed — the
  // same sentinel the backend applies. Treating absent as "not homed" would grey
  // the pad out on every machine that omits the field.
  const atHome = (axis: 'x' | 'y' | 'z') => status.axis_at_home?.[axis] !== false;
  const xyBlocked = !atHome('x') || !atHome('y');
  // ⚠️ Z is blocked here too, and that is OURS rather than BambuStudio's — it
  // sends an unhomed Z move and only advises recentering afterwards. The card's
  // bed-jog control already refuses (upstream #2579 removed its "move anyway"
  // because that drove the move with the soft endstops disabled), and offering
  // an ungated Z in this window would quietly reopen the same door.
  const zBlocked = !atHome('z');

  const temps = status.temperatures ?? {};
  const nozzleTemp = (extruderIndex === 1 ? temps.nozzle_2 : temps.nozzle) ?? 0;
  // BambuStudio's own threshold. Cold extrusion grinds a flat onto the filament.
  const tooCold = nozzleTemp < 170;

  const disabledBase = !canControl || printing || busy;

  const Btn = ({
    onClick,
    disabled,
    title,
    children,
    className = '',
  }: {
    onClick: () => void;
    disabled?: boolean;
    title?: string;
    children: React.ReactNode;
    className?: string;
  }) => (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      title={title}
      aria-label={title}
      className={`flex items-center justify-center rounded bg-bambu-dark-tertiary text-bambu-gray hover:text-white disabled:opacity-30 disabled:hover:text-bambu-gray transition-colors ${className}`}
    >
      {children}
    </button>
  );

  const xy = (axis: 'x' | 'y', distance: number) => jog.mutate({ axis, distance });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4" onClick={onClose}>
      <div
        className="bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded-lg shadow-xl w-full max-w-lg"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between px-4 py-3 border-b border-bambu-dark-tertiary">
          <h3 className="text-sm font-semibold text-white">{t('printers.motion.title')}</h3>
          <button onClick={onClose} className="text-bambu-gray hover:text-white transition-colors">
            <X className="w-4 h-4" />
          </button>
        </div>

        <div className="p-4 space-y-4">
          {printing && <p className="text-[11px] text-amber-400">{t('printers.motion.printingBlocked')}</p>}
          {!printing && (xyBlocked || zBlocked) && (
            <p className="text-[11px] text-amber-400">{t('printers.motion.notHomed')}</p>
          )}

          <div className="flex gap-5">
            <div className="flex-1 min-w-0">
              <AxisJoystick
                onMove={xy}
                onHome={() => home.mutate()}
                disabled={disabledBase}
                movesDisabled={xyBlocked}
                homeLabel={t('printers.motion.home')}
                className="w-full max-w-[220px] mx-auto"
              />

              {/* The bed row, laid out as Studio lays it: the two step sizes on
                  each side of the label, arrows pointing the way the gap goes. */}
              <div className="flex items-center justify-center gap-1 mt-3">
                {[-STEP_COARSE, -STEP_FINE].map((d) => (
                  <Btn
                    key={d}
                    onClick={() => jog.mutate({ axis: 'z', distance: d })}
                    disabled={disabledBase || zBlocked}
                    title={`Z ${d}`}
                    className="h-9 w-12 text-[11px] gap-1"
                  >
                    <ArrowUpFromLine className="w-3.5 h-3.5" /> {Math.abs(d)}
                  </Btn>
                ))}
                <span className="text-[10px] text-bambu-gray px-1">{t('printers.motion.bed')}</span>
                {[STEP_FINE, STEP_COARSE].map((d) => (
                  <Btn
                    key={d}
                    onClick={() => jog.mutate({ axis: 'z', distance: d })}
                    disabled={disabledBase || zBlocked}
                    title={`Z +${d}`}
                    className="h-9 w-12 text-[11px] gap-1"
                  >
                    <ArrowDownToLine className="w-3.5 h-3.5" /> {d}
                  </Btn>
                ))}
              </div>
            </div>

            {/* Extruder column */}
            <div className="w-[168px] shrink-0 border-l border-bambu-dark-tertiary pl-4 flex flex-col items-center">
              {isDualNozzle && (
                <div className="flex w-full rounded overflow-hidden mb-3">
                  {[0, 1].map((i) => (
                    <button
                      key={i}
                      type="button"
                      onClick={() => setExtruderIndex(i)}
                      /* ⚠️ ``min-w-0`` is what makes the halves equal. ``flex-1``
                         alone is `flex: 1 1 0%`, but a flex item defaults to
                         `min-width: auto` and so refuses to shrink below its own
                         text — which handed the wider half to whichever label was
                         longer. In Ukrainian that is "Допоміжний", and it pushed
                         its neighbour out of the column. */
                      className={`flex-1 min-w-0 h-7 px-1.5 text-[11px] leading-none text-center transition-colors ${
                        extruderIndex === i
                          ? 'bg-bambu-green text-white'
                          : 'bg-bambu-dark-tertiary text-bambu-gray hover:text-white'
                      }`}
                    >
                      {i === 0 ? t('printers.motion.main') : t('printers.motion.auxiliary')}
                    </button>
                  ))}
                </div>
              )}

              <Btn
                onClick={() => jog.mutate({ axis: 'e', distance: -STEP_COARSE })}
                disabled={disabledBase || tooCold}
                title={t('printers.motion.retract')}
                className="h-9 w-14"
              >
                <ArrowUp className="w-4 h-4" />
              </Btn>

              <ExtruderGraphic
                isDualNozzle={isDualNozzle}
                selected={extruderIndex}
                hasFilament={status.ext_has_filament ?? {}}
                className="my-2"
              />

              <Btn
                onClick={() => jog.mutate({ axis: 'e', distance: STEP_COARSE })}
                disabled={disabledBase || tooCold}
                title={t('printers.motion.extrude')}
                className="h-9 w-14"
              >
                <ArrowDown className="w-4 h-4" />
              </Btn>

              <span className="text-[10px] text-bambu-gray mt-2">{t('printers.motion.extruder')}</span>
              {tooCold && (
                <p className="text-[10px] text-bambu-gray/80 mt-1 text-center leading-snug">
                  {t('printers.motion.tooCold', { temp: Math.round(nozzleTemp) })}
                </p>
              )}
            </div>
          </div>

          <button
            type="button"
            onClick={() => release.mutate()}
            disabled={disabledBase}
            className="w-full h-8 rounded bg-bambu-dark-tertiary text-[11px] text-bambu-gray hover:text-white disabled:opacity-30 flex items-center justify-center gap-1.5 transition-colors"
          >
            <Unlock className="w-3.5 h-3.5" />
            {t('printers.motion.releaseMotors')}
          </button>

          {error && <p className="text-[11px] text-red-400 leading-relaxed">{error}</p>}
        </div>
      </div>
    </div>
  );
}
