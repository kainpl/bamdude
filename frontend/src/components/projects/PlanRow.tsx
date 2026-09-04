import { useMemo, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { api } from '../../api/client';
import type { Order, PlanRow as PlanRowData } from '../../api/client';
import { formatMoney } from '../../utils/currency';
import { formatDuration } from '../../utils/date';
import { normalizeModelName } from '../../utils/printer';
import { Button } from '../Button';
import { PrintModal } from '../PrintModal';
import { chosenPlate, parseCount, splitIsOff, type ChosenPlate } from './planMath';

/** The server's own ceiling on one enqueue item (`PlanEnqueueItem.count`). */
export const MAX_PER_PLATE = 999;

interface PlanRowProps {
  order: Order;
  lineId: number;
  row: PlanRowData;
  count: number;
  /** Which of the row's plates the operator set it to print — its own, or one
   *  of its alternatives. Undefined is the engine's own pick. */
  chosen: number | undefined;
  /** `plate_id → prints`, once the operator has opened the split and edited it.
   *  Undefined means "all on the chosen file", which is what every row does
   *  until somebody says otherwise. */
  split: Record<number, number> | undefined;
  currency: string | null | undefined;
  showCost: boolean;
  canQueue: boolean;
  canPrint: boolean;
  busy: boolean;
  onCount: (next: number) => void;
  onChoose: (plateId: number) => void;
  onSplit: (next: Record<number, number>) => void;
  onEnqueue: () => void;
  onQueued: () => void;
}

/** The row's own plate first, then its alternatives as the server sorted them.
 *  One list, used by the file switch, the printer match and the split alike. */
function plateOptions(row: PlanRowData): ChosenPlate[] {
  return [chosenPlate(row), ...row.alternatives];
}

/** `filename (X1C)`, or the bare filename when the file names no model. The
 *  model is what tells two otherwise identically-named exports apart. */
function optionLabel(plate: ChosenPlate): string {
  return plate.printer_model ? `${plate.filename} (${plate.printer_model})` : plate.filename;
}

/**
 * One recommended plate, printed `count` times.
 *
 * ⚠️ **The figures on the row are PER PRINT** — `count` is the multiplier, and
 * multiplying is this component's whole job. That is also why the count editor
 * is here rather than in a modal: the operator changes a number and reads the
 * consequence in the same line.
 *
 * ⚠️ **`plate_id` is `ProductPlate.id`, `plate_index` is the slicer's.** The
 * queue and `PrintModal` speak the second one, where 0 means "no plate pinned",
 * i.e. the whole file — hence `plate_index || undefined` below and nowhere a
 * bare `plate_index`.
 *
 * ⚠️ **A row can stand for SEVERAL files** (`row.alternatives`): the same part
 * is routinely sliced once per printer model, and the engine's greedy picks
 * one of them, which used to make the others invisible here. The switch, the
 * printer match and the split all read the same `plateOptions` list, and the
 * only thing that never moves with the choice is the COUNT — the alternatives
 * are by construction plates making the same counted parts.
 *
 * The `+` is deliberately uncapped: a count over the server's 999 disables the
 * *queue* button with the reason in its title, rather than silently refusing a
 * click or clamping a number the operator typed on purpose.
 *
 * ⚠️ **The test ids carry the LINE id beside the plate's.** `ProductPlate.id`
 * is unique per product, not per order — two lines of the same product put the
 * same plate on screen twice, and a bare `plan-row-100` would match both. The
 * `counts` state upstream has always been keyed `lineId → plateId`; this is the
 * ids catching up with it.
 */
export function PlanRow({
  order,
  lineId,
  row,
  count,
  chosen,
  split,
  currency,
  showCost,
  canQueue,
  canPrint,
  busy,
  onCount,
  onChoose,
  onSplit,
  onEnqueue,
  onQueued,
}: PlanRowProps) {
  const { t } = useTranslation();
  const [printing, setPrinting] = useState<{ plate: ChosenPlate; printerId?: number } | null>(null);
  const [pickingPrinter, setPickingPrinter] = useState(false);
  const [splitting, setSplitting] = useState(false);

  const options = plateOptions(row);
  const plate = chosenPlate(row, chosen);
  const hasAlternatives = row.alternatives.length > 0;

  // ⚠️ Gated, and the gate is the point: a plan with no alternatives asks
  // nothing about printers, which keeps "routing is not dispatching" true of
  // the ordinary block. What the list is for is matching a MODEL to a file —
  // nothing here reads a printer's state, and nothing here may start to.
  const { data: allPrinters } = useQuery({
    queryKey: ['printers'],
    queryFn: api.getPrinters,
    enabled: canPrint && hasAlternatives,
  });
  // ⚠️ Parked printers are not offered. `getPrinters` already leaves ARCHIVED
  // ones out server-side, but Maintenance Mode (`is_active === false`) is the
  // independent axis: the card stays visible on the printers page and the
  // machine takes no work, so offering it here mounts the dialog on a printer
  // whose queue nothing will ever dispatch from — and the operator is told
  // nothing until they go looking. Same rule every other "available printer"
  // list applies.
  const printers = useMemo(() => (allPrinters ?? []).filter((p) => p.is_active), [allPrinters]);

  const tooMany = count > MAX_PER_PLATE;
  const atZero = count === 0;
  // A split that does not add up is not a distribution — it is half an edit,
  // and sending it would queue a number nobody asked for. Same predicate the
  // block uses on its whole-plan button (`splitIsOff`), so the two cannot drift.
  const splitOff = splitIsOff(split, count);
  const step =
    'px-2 py-1 rounded border border-bambu-dark-tertiary text-white hover:bg-bambu-dark-tertiary disabled:opacity-40 disabled:hover:bg-transparent';

  const currentSplit = split ?? { [plate.plate_id]: count };

  /** The file this print should use, given the printer it is going to.
   *
   *  ⚠️ EXACTLY one match, or the row's own choice stands. Two files claiming
   *  the same model is a library the operator has to sort out, and picking one
   *  of them for them would send a print they never chose. */
  const fileForPrinter = (model: string | null): ChosenPlate => {
    // ⚠️ BOTH sides through the same normaliser. A printer row spells its model
    // "Bambu Lab X1 Carbon" while the 3MF the plate came from says "X1C", and
    // `mapModelCode` passes the long name straight through — so the two never
    // compared equal and this quietly returned the row's own file for every
    // printer named the long way, which is the bug the feature exists to fix.
    const wanted = normalizeModelName(model).toLowerCase();
    if (!wanted) return plate;
    const matches = options.filter(
      (o) => normalizeModelName(o.printer_model).toLowerCase() === wanted,
    );
    return matches.length === 1 ? matches[0] : plate;
  };

  return (
    <tr
      data-testid={`plan-row-${lineId}-${row.plate_id}`}
      className={`border-b border-bambu-dark-tertiary last:border-0 ${count === 0 ? 'opacity-50' : ''}`}
    >
      <td className="px-3 py-2 min-w-0">
        {hasAlternatives ? (
          <select
            data-testid={`plan-row-${lineId}-${row.plate_id}-file`}
            aria-label={t('orders.plan.row.file')}
            value={plate.plate_id}
            onChange={(e) => onChoose(Number(e.currentTarget.value))}
            className="max-w-full px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded text-white focus:border-bambu-green focus:outline-none"
          >
            {options.map((option) => (
              <option key={option.plate_id} value={option.plate_id}>
                {optionLabel(option)}
              </option>
            ))}
          </select>
        ) : (
          <p className="text-white truncate">{plate.filename}</p>
        )}
        <p className="text-xs text-bambu-gray">
          {plate.plate_index === 0
            ? t('orders.plan.row.wholeFile')
            : t('orders.plan.row.plate', { n: plate.plate_index })}
        </p>
      </td>

      <td className="px-3 py-2 text-xs text-bambu-gray">
        {row.useful.length > 0
          ? `${t('orders.plan.row.covers')} ${row.useful.map((u) => `${u.name} × ${u.count}`).join(' · ')}`
          : '—'}
      </td>

      <td className="px-3 py-2">
        <div className="inline-flex items-center gap-1">
          {/* ⚠️ A bare `−` / `+` is invisible to a screen reader and to every
              test that asks for a control by name — the glyphs carry the whole
              meaning. The title on the disabled one answers the question the
              disabling raises rather than leaving it on screen unexplained. */}
          <button
            type="button"
            data-testid={`plan-row-${lineId}-${row.plate_id}-dec`}
            className={step}
            aria-label={t('orders.plan.row.decrease')}
            title={atZero ? t('orders.plan.row.atZero') : undefined}
            disabled={atZero}
            onClick={() => onCount(count - 1)}
          >
            −
          </button>
          <input
            type="number"
            min={0}
            data-testid={`plan-row-${lineId}-${row.plate_id}-count`}
            value={count}
            aria-label={t('orders.plan.row.count')}
            onChange={(e) => onCount(parseCount(e.currentTarget.value, count))}
            className="w-16 px-2 py-1 text-right tabular-nums bg-bambu-dark border border-bambu-dark-tertiary rounded text-white focus:border-bambu-green focus:outline-none"
          />
          <button
            type="button"
            data-testid={`plan-row-${lineId}-${row.plate_id}-inc`}
            className={step}
            aria-label={t('orders.plan.row.increase')}
            onClick={() => onCount(count + 1)}
          >
            +
          </button>
        </div>
      </td>

      <td className="px-3 py-2 text-right text-bambu-gray tabular-nums whitespace-nowrap">
        {plate.print_time_seconds == null ? '—' : formatDuration(plate.print_time_seconds * count)}
      </td>

      <td className="px-3 py-2 text-right text-bambu-gray tabular-nums whitespace-nowrap">
        {plate.filament_used_grams == null ? '—' : (plate.filament_used_grams * count).toFixed(1)}
      </td>

      {showCost && (
        <td className="px-3 py-2 text-right text-bambu-gray tabular-nums whitespace-nowrap">
          {plate.cost == null ? '—' : formatMoney(plate.cost * count, currency)}
        </td>
      )}

      <td className="px-3 py-2">
        <div className="flex items-center justify-end gap-2 flex-wrap">
          {canQueue && (
            <Button
              size="sm"
              variant="outline"
              data-testid={`plan-row-${lineId}-${row.plate_id}-queue`}
              disabled={busy || atZero || tooMany || splitOff}
              title={
                tooMany
                  ? t('orders.plan.row.tooMany')
                  : atZero
                    ? t('orders.plan.row.atZero')
                    : splitOff
                      ? t('orders.plan.split.sum', { count })
                      : undefined
              }
              onClick={onEnqueue}
            >
              {t('orders.plan.row.toQueue', { count })}
            </Button>
          )}
          {canQueue && hasAlternatives && (
            <Button
              size="sm"
              variant="ghost"
              data-testid={`plan-row-${lineId}-${row.plate_id}-split`}
              onClick={() => setSplitting((open) => !open)}
            >
              {t('orders.plan.split.title')}
            </Button>
          )}
          {canPrint && (
            <Button
              size="sm"
              variant="ghost"
              data-testid={`plan-row-${lineId}-${row.plate_id}-printer`}
              onClick={() =>
                hasAlternatives ? setPickingPrinter(true) : setPrinting({ plate })
              }
            >
              {t('orders.plan.row.toPrinter')}
            </Button>
          )}
        </div>

        {/* ⚠️ The printer is asked FIRST, and only when the row stands for
            several files. `PrintModal` owns its own printer selector and
            reports no choice back, so there is no way to swap the file it was
            mounted with once a machine is picked inside it — and mounting it
            with the wrong file is exactly the bug this feature exists to fix.
            A row with one file opens the dialog straight away, as it always
            did. */}
        {pickingPrinter && (
          <div className="mt-2 flex justify-end">
            <select
              data-testid={`plan-row-${lineId}-${row.plate_id}-printer-pick`}
              aria-label={t('orders.plan.row.toPrinter')}
              value=""
              onChange={(e) => {
                const printer = printers.find((p) => p.id === Number(e.currentTarget.value));
                if (!printer) return;
                setPickingPrinter(false);
                setPrinting({ plate: fileForPrinter(printer.model), printerId: printer.id });
              }}
              className="px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded text-bambu-gray-light focus:border-bambu-green focus:outline-none"
            >
              <option value="">{t('orders.plan.row.toPrinter')}</option>
              {printers.map((printer) => (
                <option key={printer.id} value={printer.id}>
                  {printer.model ? `${printer.name} (${printer.model})` : printer.name}
                </option>
              ))}
            </select>
          </div>
        )}

        {/* One number per file, and they must add up to the row's count — the
            split moves prints between machines, it does not add or drop any.
            Everything starts on the file the row is showing, so opening this
            and closing it again changes nothing. */}
        {splitting && (
          <div
            className="mt-2 space-y-1 rounded border border-bambu-dark-tertiary bg-bambu-dark p-2 text-xs"
            data-testid={`plan-row-${lineId}-${row.plate_id}-split-panel`}
          >
            {options.map((option) => (
              <label key={option.plate_id} className="flex items-center justify-end gap-2">
                <span className="text-bambu-gray-light truncate">{optionLabel(option)}</span>
                <input
                  type="number"
                  min={0}
                  data-testid={`plan-row-${lineId}-${row.plate_id}-split-${option.plate_id}`}
                  value={currentSplit[option.plate_id] ?? 0}
                  onChange={(e) =>
                    onSplit({
                      ...currentSplit,
                      [option.plate_id]: parseCount(e.currentTarget.value, currentSplit[option.plate_id] ?? 0),
                    })
                  }
                  className="w-16 px-2 py-1 text-right tabular-nums bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white focus:border-bambu-green focus:outline-none"
                />
              </label>
            ))}
            {splitOff && (
              <p className="text-right text-amber-300" data-testid={`plan-row-${lineId}-${row.plate_id}-split-error`}>
                {t('orders.plan.split.sum', { count })}
              </p>
            )}
            <div className="flex justify-end">
              <Button
                size="sm"
                variant="outline"
                data-testid={`plan-row-${lineId}-${row.plate_id}-split-apply`}
                disabled={busy || atZero || tooMany || splitOff}
                onClick={onEnqueue}
              >
                {t('orders.plan.split.apply')}
              </Button>
            </div>
          </div>
        )}

        {printing && (
          <PrintModal
            mode="add-to-queue"
            libraryFileId={printing.plate.library_file_id}
            archiveName={printing.plate.filename}
            preselectedPlateId={printing.plate.plate_index || undefined}
            projectId={order.id}
            projectLineId={lineId}
            // Pinned, not hidden: the operator named the machine in the row's
            // own menu, and the dialog should still say which one it is.
            initialSelectedPrinterIds={printing.printerId == null ? undefined : [printing.printerId]}
            lockPrinterSelection={printing.printerId != null}
            // Routing, not dispatching: the modal is opened on the printer leg
            // and kept there, because "to printer…" already answered the only
            // question the toggle asks.
            initialDispatchMode="specific"
            lockDispatchMode
            onClose={() => setPrinting(null)}
            onSuccess={() => {
              setPrinting(null);
              onQueued();
            }}
          />
        )}
      </td>
    </tr>
  );
}
