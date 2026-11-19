import { useEffect, useMemo, useState } from 'react';
import { NumberInput } from '../NumberInput';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { AlertTriangle, CheckCircle2, Cog, LayoutGrid, Loader2, Minus, Plus, Trash2, XCircle } from 'lucide-react';

import {
  api,
  ApiError,
  type AssemblyJobResult,
  type AssemblyJobState,
  type AssemblyPrinterResult,
  type AssemblyProgress,
  type LibraryFileListItem,
  type PresetRef,
  type UnifiedPresetsResponse,
} from '../../api/client';
import { useSliceJobTracker } from '../../contexts/SliceJobTrackerContext';
import { resolvePresetName } from '../../utils/presetPickerUtils';
import { findPreset, pickPrinterDefault, pickFilamentForSlot, pickProcessDefault } from '../../utils/slicePresetPicker';
import { buildCompatibilityIndex, presetCompatibility, type PrinterCompatibilityIndex } from '../../utils/slicerPrinterMatch';
import { Button } from '../Button';
import { Modal } from '../Modal';
import { Select } from '../Select';
import { Toggle } from '../Toggle';
import { usePresetVisibility } from '../../hooks/usePresetVisibility';
import { PresetVisibilityFilters } from '../PresetVisibilityFilters';
import { AssemblePresetSelect } from './AssemblePresetSelect';
import {
  MAX_COPIES,
  MAX_TARGETS,
  ORIENTATIONS,
  ROTATION_STEPS,
  emptyTarget,
  formFromRecord,
  initialForm,
  toRelayoutRequest,
  toRequest,
  validate,
  type AssembleForm,
  type PartRow,
  type TargetRow,
} from './assembleForm';

export type AssembleMode =
  | { kind: 'new'; files: LibraryFileListItem[] }
  | { kind: 'relayout'; fileId: number; filename: string };

interface AssembleProjectModalProps {
  mode: AssembleMode;
  onClose: () => void;
  /** Show one of the written files in the library (the summary's file links). */
  onOpenFile?: (fileId: number) => void;
  /**
   * Open the Slice dialog for one of the written files (the summary's "Slice"). Given only where the page
   * offers in-app slicing to this user; the dialog reads the printer, process and filament the assembly
   * embedded off the file itself, so nothing about them is handed over.
   */
  onSliceFile?: (fileId: number, name: string) => void;
}

const POLL_MS = 1500;
const INPUT =
  'px-3 py-2 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm focus:outline-none focus:border-bambu-gray';

/**
 * A chosen preset the listing no longer holds — a re-layout opens with the presets the file was made with,
 * and one of them may have been deleted since. Its select shows nothing, so the row has to say it.
 */
function targetPresetGone(data: UnifiedPresetsResponse, target: TargetRow): boolean {
  return (['printer', 'process', 'filament'] as const).some(
    (slot) => target[slot] !== null && resolvePresetName(data, target[slot], slot) === null,
  );
}

const printerInfoQuery = (printer: PresetRef | null) => ({
  queryKey: ['assemblyPrinterInfo', printer?.source, printer?.id],
  queryFn: () => api.getAssemblyPrinterInfo(printer as PresetRef),
  enabled: printer !== null,
  retry: false,
  staleTime: 5 * 60_000,
});

/** The presets a new target row starts with: the first printer, then what suits it. */
function defaultsFor(
  data: UnifiedPresetsResponse,
  printer: PresetRef | null,
  compatIndex: PrinterCompatibilityIndex,
  onlyMine = false,
): Pick<TargetRow, 'printer' | 'process' | 'filament'> {
  const chosen = printer ?? pickPrinterDefault(data);
  const printerName = resolvePresetName(data, chosen, 'printer');
  const fits = (ref: PresetRef | null, slot: 'process' | 'filament') => {
    const preset = findPreset(data, ref, slot);
    return !onlyMine || (preset && presetCompatibility(preset, slot, printerName, compatIndex) !== 'mismatch') ? ref : null;
  };
  return {
    printer: chosen,
    process: fits(pickProcessDefault(data, printerName, compatIndex), 'process'),
    filament: fits(pickFilamentForSlot(data, { type: 'PLA', color: '' }, printerName, compatIndex), 'filament'),
  };
}

function TargetEditor({
  target,
  index,
  data,
  compatIndex,
  visibility,
  removable,
  onChange,
  onRemove,
}: {
  target: TargetRow;
  index: number;
  data: UnifiedPresetsResponse;
  compatIndex: PrinterCompatibilityIndex;
  visibility: ReturnType<typeof usePresetVisibility>;
  removable: boolean;
  onChange: (next: TargetRow) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const printerName = resolvePresetName(data, target.printer, 'printer');
  const info = useQuery(printerInfoQuery(target.printer));
  const nozzles = info.data?.nozzles ?? [];
  const presetGone = targetPresetGone(data, target);

  // A printer with one nozzle has nothing to choose; one that lost the chosen nozzle falls back to the first.
  useEffect(() => {
    if (info.data && !info.data.nozzles.some((nozzle) => nozzle.index === target.nozzle)) {
      onChange({ ...target, nozzle: info.data.nozzles[0]?.index ?? 1 });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new answer re-checks the nozzle
  }, [info.data]);

  return (
    <div className="rounded-lg border border-bambu-dark-tertiary p-3 space-y-2" data-testid={`assemble-target-${index}`}>
      <div className="flex items-center justify-between">
        <span className="text-sm text-white">
          {t('fileManager.assemble.targetTitle', { number: index + 1 })}
          {info.data?.model ? <span className="text-bambu-gray"> — {info.data.model}</span> : null}
        </span>
        {removable && (
          <button
            type="button"
            className="p-1 text-bambu-gray hover:text-red-400"
            onClick={onRemove}
            aria-label={t('fileManager.assemble.removeTarget', { number: index + 1 })}
          >
            <Trash2 className="w-4 h-4" />
          </button>
        )}
      </div>
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        <AssemblePresetSelect
          label={t('fileManager.assemble.printerPreset')}
          slot="printer"
          data={data}
          keep={visibility.keep}
          value={target.printer}
          onChange={(printer) => onChange({ ...target, ...defaultsFor(visibility.visible ?? data, printer, compatIndex, visibility.onlyMine), printer, nozzle: 1 })}
        />
        <AssemblePresetSelect
          label={t('fileManager.assemble.processPreset')}
          slot="process"
          data={data}
          keep={visibility.keep}
          value={target.process}
          onChange={(process) => onChange({ ...target, process })}
          selectedPrinterName={printerName}
          compatIndex={compatIndex}
        />
        <AssemblePresetSelect
          label={t('fileManager.assemble.filamentPreset')}
          slot="filament"
          data={data}
          keep={visibility.keep}
          value={target.filament}
          onChange={(filament) => onChange({ ...target, filament })}
          selectedPrinterName={printerName}
          compatIndex={compatIndex}
        />
      </div>
      {nozzles.length > 1 && (
        <label className="flex items-center gap-2 text-xs text-bambu-gray">
          <span>{t('fileManager.assemble.nozzle')}</span>
          <Select
            value={String(target.nozzle)}
            onChange={(event) => onChange({ ...target, nozzle: Number(event.target.value) })}
            aria-label={t('fileManager.assemble.nozzleFor', { number: index + 1 })}
          >
            {nozzles.map((nozzle) => (
              <option key={nozzle.index} value={nozzle.index}>
                {nozzles.length === 2
                  ? t(nozzle.index === 1 ? 'fileManager.assemble.nozzleLeft' : 'fileManager.assemble.nozzleRight')
                  : t('fileManager.assemble.nozzleNumber', { number: nozzle.index })}
              </option>
            ))}
          </Select>
        </label>
      )}
      {presetGone && (
        <p className="text-xs text-amber-400 flex items-center gap-1">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          {t('fileManager.assemble.presetGone')}
        </p>
      )}
      {info.error instanceof ApiError && (
        <p className="text-xs text-amber-400 flex items-center gap-1">
          <AlertTriangle className="w-3.5 h-3.5 shrink-0" />
          {info.error.message}
        </p>
      )}
    </div>
  );
}

function PartEditor({ part, index, onChange }: { part: PartRow; index: number; onChange: (next: PartRow) => void }) {
  const { t } = useTranslation();
  const clamp = (value: number) => Math.min(MAX_COPIES, Math.max(1, Math.round(value) || 1));
  return (
    <tr className="border-t border-bambu-dark-tertiary" data-testid={`assemble-part-${index}`}>
      <td className="py-2 pr-2 text-sm text-white break-all">{part.name}</td>
      <td className="py-2 pr-2">
        <div className="flex items-center gap-1">
          <button
            type="button"
            className="p-1.5 rounded bg-bambu-dark border border-bambu-dark-tertiary text-white disabled:opacity-40"
            onClick={() => onChange({ ...part, quantity: clamp(part.quantity - 1) })}
            disabled={part.fill || part.quantity <= 1}
            aria-label={t('fileManager.assemble.fewer', { name: part.name })}
          >
            <Minus className="w-3 h-3" />
          </button>
          <NumberInput
            min={1}
            max={MAX_COPIES}
            className={`${INPUT} w-20 text-center disabled:opacity-40`}
            value={part.fill ? null : part.quantity}
            disabled={part.fill}
            onChange={(v) => onChange({ ...part, quantity: clamp(v) })}
            fallback={1}
            aria-label={t('fileManager.assemble.quantityOf', { name: part.name })}
          />
          <button
            type="button"
            className="p-1.5 rounded bg-bambu-dark border border-bambu-dark-tertiary text-white disabled:opacity-40"
            onClick={() => onChange({ ...part, quantity: clamp(part.quantity + 1) })}
            disabled={part.fill || part.quantity >= MAX_COPIES}
            aria-label={t('fileManager.assemble.more', { name: part.name })}
          >
            <Plus className="w-3 h-3" />
          </button>
        </div>
      </td>
      <td className="py-2 pr-2">
        <label className="flex items-center gap-2 text-sm text-white whitespace-nowrap">
          <input
            type="checkbox"
            checked={part.fill}
            onChange={(event) => onChange({ ...part, fill: event.target.checked })}
            aria-label={t('fileManager.assemble.fillOf', { name: part.name })}
          />
          {t('fileManager.assemble.fill')}
        </label>
      </td>
      <td className="py-2">
        <Select
          value={part.orientation}
          onChange={(event) => onChange({ ...part, orientation: event.target.value as PartRow['orientation'] })}
          aria-label={t('fileManager.assemble.orientationOf', { name: part.name })}
        >
          {ORIENTATIONS.map((orientation) => (
            <option key={orientation} value={orientation}>
              {t(`fileManager.assemble.orientation.${orientation}`)}
            </option>
          ))}
        </Select>
      </td>
    </tr>
  );
}

function progressLine(t: ReturnType<typeof useTranslation>['t'], progress: AssemblyProgress | null): string {
  if (!progress) return t('fileManager.assemble.progress.preparing');
  if (progress.stage === 'waiting') {
    return t('fileManager.assemble.progress.waiting', { position: progress.position ?? 1 });
  }
  if (progress.stage === 'converting') {
    return t('fileManager.assemble.progress.converting', {
      file: progress.convert ?? 1,
      files: progress.convert_count ?? 1,
    });
  }
  if (progress.stage === 'packing') {
    if (!progress.printer_count) return t('fileManager.assemble.progress.starting');
    const where = { printer: (progress.printer_index ?? 0) + 1, printers: progress.printer_count };
    // a filled plate has no number to reach: the packer's own ceiling is not a total to show
    return progress.fill_part != null
      ? t('fileManager.assemble.progress.filling', { ...where, placed: progress.placed ?? 0 })
      : t('fileManager.assemble.progress.packing', { ...where, placed: progress.placed ?? 0, total: progress.total ?? 0 });
  }
  return t(`fileManager.assemble.progress.${progress.stage}`);
}

function reasonText(t: ReturnType<typeof useTranslation>['t'], reason: string): string {
  return t(`fileManager.assemble.reason.${reason}`, {
    defaultValue: t('fileManager.assemble.reason.unknown', { reason }),
  });
}

function PrinterSummary({
  row,
  partNames,
  onOpenFile,
  onSliceFile,
}: {
  row: AssemblyPrinterResult;
  partNames: string[];
  onOpenFile?: (fileId: number) => void;
  onSliceFile?: (fileId: number, name: string) => void;
}) {
  const { t } = useTranslation();
  const good = row.status === 'ok';
  return (
    <div className="rounded-lg border border-bambu-dark-tertiary p-3 space-y-2" data-testid="assemble-summary-row">
      <div className="flex items-center gap-2 text-sm text-white">
        {good ? (
          <CheckCircle2 className="w-4 h-4 text-bambu-green shrink-0" />
        ) : (
          <XCircle className="w-4 h-4 text-red-400 shrink-0" />
        )}
        <span>{row.model ? `${row.model} — ${row.label}` : row.label}</span>
      </div>
      {!good && (
        <div className="text-sm text-red-300">
          <p>
            {reasonText(t, row.reason)}
            {row.detail ? `: ${row.detail}` : ''}
          </p>
          {row.parts.length > 0 && (
            <ul className="list-disc ml-5 mt-1">
              {row.parts.map((part) => (
                <li key={part.index}>
                  {part.name} — {reasonText(t, part.reason)}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
      {row.files.length > 0 && (
        <ul className="text-sm space-y-1">
          {row.files.map((file) => (
            <li key={file.library_file_id}>
              <span className="flex items-start justify-between gap-3">
                {onOpenFile ? (
                  <button
                    type="button"
                    className="text-bambu-green hover:underline text-left break-all"
                    onClick={() => onOpenFile(file.library_file_id)}
                  >
                    {file.name}
                  </button>
                ) : (
                  <span className="text-white break-all">{file.name}</span>
                )}
                {onSliceFile && (
                  <button
                    type="button"
                    className="shrink-0 inline-flex items-center gap-1.5 rounded-md border border-bambu-dark-tertiary px-2 py-1 text-xs text-white hover:border-bambu-gray transition-colors"
                    aria-label={t('fileManager.assemble.sliceFile', { name: file.name })}
                    onClick={() => onSliceFile(file.library_file_id, file.name)}
                  >
                    <Cog className="w-3.5 h-3.5" />
                    {t('slice.action')}
                  </button>
                )}
              </span>
              {file.fallback_reason && (
                <span className="block text-xs text-amber-400">
                  {t(`fileManager.assemble.fallback.${file.fallback_reason}`, {
                    defaultValue: t('fileManager.assemble.fallback.other'),
                  })}
                </span>
              )}
            </li>
          ))}
        </ul>
      )}
      {row.plates.length > 0 && (
        <div className="grid grid-cols-2 md:grid-cols-3 gap-2">
          {row.plates.map((plate) => (
            <figure key={plate.plate} className="rounded bg-bambu-dark p-2">
              {plate.picture && (
                <img
                  src={plate.picture}
                  alt={t('fileManager.assemble.plateAlt', { number: plate.plate + 1 })}
                  className="w-full rounded"
                />
              )}
              <figcaption className="mt-1 text-xs text-bambu-gray">
                <span className="text-white">{t('fileManager.assemble.plate', { number: plate.plate + 1 })}</span>
                {Object.entries(plate.counts).map(([part, copies]) => (
                  <span key={part} className="block">
                    {plate.fill
                      ? `${partNames[Number(part)] ?? part} — ${t('fileManager.assemble.perPlate', { count: copies })}`
                      : `${partNames[Number(part)] ?? part} × ${copies}`}
                  </span>
                ))}
              </figcaption>
            </figure>
          ))}
        </div>
      )}
    </div>
  );
}

export function AssembleProjectModal({ mode, onClose, onOpenFile, onSliceFile }: AssembleProjectModalProps) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { trackJob } = useSliceJobTracker();
  const relayout = mode.kind === 'relayout';

  const presets = useQuery({ queryKey: ['slicerPresets'], queryFn: () => api.getSlicerPresets() });
  const visibility = usePresetVisibility(presets.data);
  const printerModels = useQuery({ queryKey: ['slicerPrinterModels'], queryFn: api.getPrinterModels });
  const compatIndex = useMemo(() => buildCompatibilityIndex(printerModels.data ?? {}), [printerModels.data]);
  const record = useQuery({
    queryKey: ['assemblyRecord', relayout ? mode.fileId : null],
    queryFn: () => api.getAssemblyRecord((mode as { fileId: number }).fileId),
    enabled: relayout,
    retry: false,
  });

  const [form, setForm] = useState<AssembleForm | null>(() => (mode.kind === 'new' ? initialForm(mode.files) : null));
  const [seeded, setSeeded] = useState(false);
  const [jobId, setJobId] = useState<number | null>(null);
  const [submitError, setSubmitError] = useState<string | null>(null);
  const [finished, setFinished] = useState<AssemblyJobState | null>(null);

  // A re-layout's form is the record's; a new one's first target gets its presets once they are listed.
  useEffect(() => {
    if (relayout && record.data && form === null) setForm(formFromRecord(record.data));
  }, [relayout, record.data, form]);
  // Every change goes through the current form, never the one a handler closed over: two rows may report in
  // one commit (each printer's nozzle answer), and the second must not undo the first.
  const update = (change: (current: AssembleForm) => AssembleForm) =>
    setForm((current) => (current ? change(current) : current));
  const data = presets.data;
  const modelsSettled = printerModels.isFetched || printerModels.isError;
  useEffect(() => {
    // wait for the printer-model registry too: without it nothing is known to suit the printer
    if (seeded || !data || !form || !modelsSettled || !visibility.ready) return;
    setSeeded(true);
    if (!relayout) {
      setForm((current) =>
        current
          ? {
              ...current,
              targets: current.targets.map((target) =>
                target.printer ? target : { ...target, ...defaultsFor(visibility.visible ?? data, null, compatIndex, visibility.onlyMine) },
              ),
            }
          : current,
      );
    }
  }, [seeded, data, form, relayout, compatIndex, modelsSettled, visibility.ready, visibility.visible, visibility.onlyMine]);

  const issues = form ? validate(form) : [];
  // the same queries the rows show their warning from: a printer preset that cannot be used blocks the button
  const printerInfos = useQueries({ queries: (form?.targets ?? []).map((target) => printerInfoQuery(target.printer)) });
  const printerUnusable =
    printerInfos.some((info) => info.isError) ||
    (data !== undefined && (form?.targets ?? []).some((target) => targetPresetGone(data, target)));
  const submit = useMutation({
    mutationFn: () =>
      relayout
        ? api.relayoutFile(mode.fileId, toRelayoutRequest(form as AssembleForm))
        : api.assembleProject(toRequest(form as AssembleForm)),
    onMutate: () => setSubmitError(null),
    onSuccess: (job) => setJobId(job.job_id),
    onError: (error) =>
      setSubmitError(error instanceof ApiError ? error.message : t('fileManager.assemble.submitFailed')),
  });

  const job = useQuery({
    queryKey: ['assemblyJob', jobId],
    queryFn: () => api.getAssemblyJob(jobId as number),
    enabled: jobId !== null && finished === null,
    refetchInterval: POLL_MS,
    retry: false,
  });
  useEffect(() => {
    const state = job.data;
    if (state && (state.status === 'completed' || state.status === 'failed') && finished === null) {
      setFinished(state);
      queryClient.invalidateQueries({ queryKey: ['library-files'] });
    }
  }, [job.data, finished, queryClient]);

  const stop = useMutation({ mutationFn: () => api.cancelSliceJob(jobId as number) });

  const phase: 'form' | 'running' | 'summary' = finished ? 'summary' : jobId !== null ? 'running' : 'form';
  const close = () => {
    // closing does not stop the job: the tracker's toast takes it from here
    if (phase === 'running' && jobId !== null && form) trackJob(jobId, 'assembly', form.name);
    onClose();
  };

  const setPart = (index: number, next: PartRow) =>
    update((current) => ({ ...current, parts: current.parts.map((part, i) => (i === index ? next : part)) }));
  const setTarget = (index: number, next: TargetRow) =>
    update((current) => ({
      ...current,
      targets: current.targets.map((target, i) => (i === index ? next : target)),
    }));

  const title = relayout ? t('fileManager.assemble.relayoutTitle') : t('fileManager.assemble.title');
  const result: AssemblyJobResult | undefined = finished?.result;
  const canceled = finished?.status === 'failed' && finished.error_status === 499;

  let body;
  let footer;
  if (phase === 'summary') {
    const partNames = form ? form.parts.map((part) => part.name) : [];
    body = (
      <div className="space-y-3" data-testid="assemble-summary">
        {canceled && <p className="text-sm text-amber-400">{t('fileManager.assemble.stopped')}</p>}
        {finished?.status === 'failed' && !canceled && (
          <p className="text-sm text-red-300">{finished.error_detail || t('fileManager.assemble.failed')}</p>
        )}
        {result && result.outcome === 'failed' && (
          <div className="text-sm text-red-300">
            <p>{reasonText(t, result.reason)}</p>
            {result.parts.length > 0 && (
              <ul className="list-disc ml-5 mt-1">
                {result.parts.map((part) => (
                  <li key={part.index}>
                    {part.name} — {reasonText(t, part.reason)}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {result && result.outcome === 'ok' && result.printers.every((row) => row.status === 'ok') && (
          <p className="text-sm text-bambu-green">{t('fileManager.assemble.done')}</p>
        )}
        {result && result.outcome === 'ok' && result.printers.some((row) => row.status !== 'ok') && (
          <p className="text-sm text-amber-400">{t('fileManager.assemble.doneInPart')}</p>
        )}
        {result?.printers.map((row, index) => (
          <PrinterSummary
            key={index}
            row={row}
            partNames={partNames}
            onOpenFile={onOpenFile}
            onSliceFile={onSliceFile}
          />
        ))}
        {result && result.trashed.length > 0 && (
          <p className="text-xs text-bambu-gray">{t('fileManager.assemble.trashed', { count: result.trashed.length })}</p>
        )}
        {result && result.removed.length > 0 && (
          <p className="text-xs text-bambu-gray">{t('fileManager.assemble.removed', { count: result.removed.length })}</p>
        )}
        {result && result.kept.length > 0 && (
          <p className="text-xs text-amber-400">{t('fileManager.assemble.kept', { count: result.kept.length })}</p>
        )}
      </div>
    );
    footer = (
      <>
        <Button variant="secondary" onClick={() => {
          // Keep the form, retire the old poll and clear cached mutation state.
          setJobId(null); setFinished(null); setSubmitError(null); submit.reset(); stop.reset();
          update((current) => ({ ...current, trashPrevious: false }));
        }}>{t('fileManager.assemble.editAndRetry')}</Button>
        <Button variant="primary" onClick={onClose}>{t('common.close')}</Button>
      </>
    );
  } else if (phase === 'running') {
    body = (
      <div className="flex flex-col items-center gap-3 py-8" data-testid="assemble-running">
        <Loader2 className="w-8 h-8 animate-spin text-bambu-green" />
        <p className="text-sm text-white" role="status">
          {progressLine(t, job.data?.progress ?? null)}
        </p>
        {job.isError ? (
          <p className="text-sm text-red-300" role="alert">
            {t('fileManager.assemble.pollFailed')}
          </p>
        ) : (
          <p className="text-xs text-bambu-gray">{t('fileManager.assemble.runsInBackground')}</p>
        )}
        {stop.error instanceof ApiError && (
          <p className="text-sm text-amber-400" role="alert">
            {stop.error.message}
          </p>
        )}
      </div>
    );
    footer = (
      <>
        <Button variant="secondary" onClick={close}>
          {t('common.close')}
        </Button>
        <Button
          variant="danger"
          onClick={() => stop.mutate()}
          disabled={stop.isPending || stop.isSuccess || job.data?.progress?.stage === 'publishing'}
        >
          {t('fileManager.assemble.stop')}
        </Button>
      </>
    );
  } else if (relayout && record.error) {
    body = (
      <p className="text-sm text-red-300">
        {record.error instanceof ApiError ? record.error.message : t('fileManager.assemble.recordFailed')}
      </p>
    );
    footer = (
      <Button variant="secondary" onClick={onClose}>
        {t('common.close')}
      </Button>
    );
  } else if (!form || !data) {
    body = (
      <div className="flex justify-center py-8">
        <Loader2 className="w-6 h-6 animate-spin text-bambu-gray" />
      </div>
    );
    footer = (
      <Button variant="secondary" onClick={onClose}>
        {t('common.cancel')}
      </Button>
    );
  } else {
    body = (
      <div className="space-y-5">
        <section>
          <h3 className="text-sm font-medium text-white mb-2">{t('fileManager.assemble.parts')}</h3>
          <table className="w-full">
            <thead>
              <tr className="text-left text-xs text-bambu-gray">
                <th className="font-normal pb-1">{t('fileManager.assemble.part')}</th>
                <th className="font-normal pb-1">{t('fileManager.assemble.quantity')}</th>
                <th className="font-normal pb-1" />
                <th className="font-normal pb-1">{t('fileManager.assemble.orientationLabel')}</th>
              </tr>
            </thead>
            <tbody>
              {form.parts.map((part, index) => (
                <PartEditor key={index} part={part} index={index} onChange={(next) => setPart(index, next)} />
              ))}
            </tbody>
          </table>
          {issues.includes('copies') && (
            <p className="mt-1 text-xs text-red-300">{t('fileManager.assemble.issue.copies', { max: MAX_COPIES })}</p>
          )}
        </section>

        <section className="space-y-2">
          <div className="flex items-center justify-between">
            <h3 className="text-sm font-medium text-white">{t('fileManager.assemble.targets')}</h3>
            <Button
              variant="secondary"
              size="sm"
              onClick={() =>
                update((current) => ({
                  ...current,
                  targets: [...current.targets, { ...emptyTarget(), ...defaultsFor(visibility.visible ?? data, null, compatIndex, visibility.onlyMine) }],
                }))
              }
              disabled={form.targets.length >= MAX_TARGETS}
            >
              <Plus className="w-4 h-4 mr-1" />
              {t('fileManager.assemble.addTarget')}
            </Button>
          </div>
          <p className="text-xs text-bambu-gray">{t('fileManager.assemble.targetsHint')}</p>
          <PresetVisibilityFilters filters={visibility} disabled={submit.isPending} />
          {form.targets.map((target, index) => (
            <TargetEditor
              key={target.key}
              target={target}
              index={index}
              data={data}
              compatIndex={compatIndex}
              visibility={visibility}
              removable={form.targets.length > 1}
              onChange={(next) => setTarget(index, next)}
              onRemove={() =>
                update((current) => ({ ...current, targets: current.targets.filter((_, i) => i !== index) }))
              }
            />
          ))}
        </section>

        <section className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <label className="block text-xs text-bambu-gray">
            {t('fileManager.assemble.spacing')}
            <input
              type="text"
              inputMode="decimal"
              className={`${INPUT} w-full mt-1`}
              value={form.spacing}
              onChange={(event) => update((current) => ({ ...current, spacing: event.target.value }))}
              aria-invalid={issues.includes('spacing')}
            />
          </label>
          <label className="block text-xs text-bambu-gray">
            {t('fileManager.assemble.rotation')}
            <Select
              className="w-full mt-1"
              value={String(form.rotationStep)}
              onChange={(event) =>
                update((current) => ({ ...current, rotationStep: Number(event.target.value) as AssembleForm['rotationStep'] }))
              }
            >
              {ROTATION_STEPS.map((step) => (
                <option key={step} value={step}>
                  {t(`fileManager.assemble.rotationStep.${step}`)}
                </option>
              ))}
            </Select>
          </label>
          <div className="flex items-center justify-between gap-3 text-sm text-white">
            <span>{t('fileManager.assemble.centre')}</span>
            <Toggle
              checked={form.centre}
              onChange={(centre) => update((current) => ({ ...current, centre }))}
              label={t('fileManager.assemble.centre')}
            />
          </div>
          <div className="flex items-center justify-between gap-3 text-sm text-white">
            <span>{t('fileManager.assemble.splitPlates')}</span>
            <Toggle
              checked={form.splitPlates}
              onChange={(splitPlates) => update((current) => ({ ...current, splitPlates }))}
              label={t('fileManager.assemble.splitPlates')}
            />
          </div>
        </section>

        <section className="space-y-2">
          <label className="block text-xs text-bambu-gray">
            {t('fileManager.assemble.name')}
            <input
              type="text"
              maxLength={120}
              className={`${INPUT} w-full mt-1`}
              value={form.name}
              onChange={(event) => update((current) => ({ ...current, name: event.target.value }))}
              aria-invalid={issues.includes('name')}
            />
          </label>
          {relayout && (
            <label className="flex items-center gap-2 text-sm text-white">
              <input
                type="checkbox"
                checked={form.trashPrevious}
                onChange={(event) => update((current) => ({ ...current, trashPrevious: event.target.checked }))}
              />
              {t('fileManager.assemble.trashPrevious')}
            </label>
          )}
        </section>
      </div>
    );
    footer = (
      <>
        <Button variant="secondary" onClick={onClose} disabled={submit.isPending}>
          {t('common.cancel')}
        </Button>
        <Button variant="primary" onClick={() => submit.mutate()} disabled={issues.length > 0 || printerUnusable || submit.isPending}>
          {submit.isPending && <Loader2 className="w-4 h-4 mr-1 animate-spin" />}
          {relayout ? t('fileManager.assemble.relayoutSubmit') : t('fileManager.assemble.submit')}
        </Button>
      </>
    );
  }

  return (
    <Modal
      onClose={close}
      closeDisabled={submit.isPending}
      title={title}
      icon={<LayoutGrid className="w-5 h-5" />}
      size="3xl"
      bodyClassName="p-4"
      subheader={
        relayout ? <p className="px-4 pb-2 text-xs text-bambu-gray break-all">{mode.filename}</p> : undefined
      }
      alert={
        submitError ? (
          <p className="px-4 py-2 text-sm text-red-300" role="alert">
            {submitError}
          </p>
        ) : undefined
      }
      footer={footer}
    >
      {body}
    </Modal>
  );
}
