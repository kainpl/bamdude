import { useEffect, useMemo, useRef, useState } from 'react';
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
  type SlicerPipeline,
  type UnifiedPresetsResponse,
} from '../../api/client';
import { useSliceJobTracker } from '../../contexts/SliceJobTrackerContext';
import { buildParentsBySource, buildPrinterParents } from '../../utils/printerPresetParents';
import {
  findPreset,
  pickFilamentForSlot,
  pickPrinterFallback,
  pickProcessDefault,
} from '../../utils/slicePresetPicker';
import { buildCompatibilityIndex, presetCompatibility } from '../../utils/slicerPrinterMatch';
import { Button } from '../Button';
import { Modal } from '../Modal';
import { Select } from '../Select';
import { Toggle } from '../Toggle';
import { PipelineSelect } from '../slicing-setup/PipelineSelect';
import { PresetFilters } from '../slicing-setup/PresetFilters';
import { PresetListStatus } from '../slicing-setup/PresetListStatus';
import { PresetSelect } from '../slicing-setup/PresetSelect';
import { applyPipeline } from '../slicing-setup/applyPipeline';
import { POLICIES, useChoiceFields, type ChoicePolicy } from '../slicing-setup/choiceState';
import { buildPresetField } from '../slicing-setup/presetSets';
import { usePresetCatalogue } from '../slicing-setup/usePresetCatalogue';
import { usePresetFilters, type PresetFiltersState } from '../slicing-setup/usePresetFilters';
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

const printerInfoQuery = (printer: PresetRef | null) => ({
  queryKey: ['assemblyPrinterInfo', printer?.source, printer?.id],
  queryFn: () => api.getAssemblyPrinterInfo(printer as PresetRef),
  enabled: printer !== null,
  retry: false,
  staleTime: 5 * 60_000,
});

const SLOTS = ['printer', 'process', 'filament'] as const;
type RowSlot = (typeof SLOTS)[number];
const POLICY: Record<RowSlot, ChoicePolicy> = {
  printer: POLICIES.assemblyPrinter,
  process: POLICIES.assemblyProcess,
  filament: POLICIES.assemblyFilament,
};
/** A row's field in the choice state: keyed by the row's stable key, so removing a row moves nothing. */
const fieldKey = (rowKey: string, slot: RowSlot) => `${rowKey}:${slot}`;

/** The printer name and compatibility index a row's process and filament are judged against. */
function rowContext(data: UnifiedPresetsResponse, printer: PresetRef | null, models: Record<string, string>) {
  const name = printer ? (findPreset(data, printer, 'printer')?.name ?? null) : null;
  const compat = buildCompatibilityIndex(models, buildPrinterParents(data, printer?.source ?? null));
  return { name, compat };
}

/**
 * A row's automatic picks (spec slicing-setup R5): the printer by the Slice
 * dialog's last step (a 0.4 machine preset first, never a model card), the
 * process by `pickProcessDefault` on the whole catalogue, the filament by the
 * default PLA — a row has no material of its own.
 */
function defaultsFor(
  data: UnifiedPresetsResponse,
  printer: PresetRef | null,
  models: Record<string, string>,
): Record<RowSlot, PresetRef | null> {
  const chosen = printer ?? pickPrinterFallback(data, buildParentsBySource(data));
  const { name, compat } = rowContext(data, chosen, models);
  return {
    printer: chosen,
    process: pickProcessDefault(data, name, compat),
    filament: pickFilamentForSlot(data, { type: 'PLA', color: '' }, name, compat),
  };
}

/** Whether an automatic process / filament still fits the row's printer. */
function usableFor(data: UnifiedPresetsResponse, slot: 'process' | 'filament', printer: PresetRef | null, models: Record<string, string>) {
  const { name, compat } = rowContext(data, printer, models);
  return (ref: PresetRef) => {
    const p = findPreset(data, ref, slot);
    return p !== null && presetCompatibility(p, slot, name, compat) !== 'mismatch';
  };
}

function TargetEditor({
  target,
  index,
  data,
  models,
  refs,
  gone,
  filters,
  parentsBySource,
  removable,
  onPick,
  onApplySet,
  onNozzle,
  onRemove,
}: {
  target: TargetRow;
  index: number;
  data: UnifiedPresetsResponse;
  models: Record<string, string>;
  refs: Record<RowSlot, PresetRef | null>;
  gone: Record<RowSlot, boolean>;
  filters: PresetFiltersState;
  parentsBySource: ReturnType<typeof buildParentsBySource>;
  removable: boolean;
  onPick: (slot: RowSlot, ref: PresetRef | null) => void;
  onApplySet: (set: SlicerPipeline) => { name: string; changed: string[]; unresolved: string[] };
  onNozzle: (nozzle: number) => void;
  onRemove: () => void;
}) {
  const { t } = useTranslation();
  const info = useQuery(printerInfoQuery(refs.printer));
  const nozzles = info.data?.nozzles ?? [];
  const presetGone = SLOTS.some((slot) => gone[slot]);
  const [applied, setApplied] = useState<{ name: string; changed: string[] } | null>(null);
  const [unresolved, setUnresolved] = useState<string[]>([]);

  // The row's own printer judges its process and filament, and its loaded test (spec §7.3).
  const { name: printerName, compat } = useMemo(() => rowContext(data, refs.printer, models), [data, refs.printer, models]);
  const fields = useMemo(() => {
    const softFilters = (slot: RowSlot) => filters.softFilters({ slot, printerName, parentsBySource });
    const fit = { printerName, compatIndex: compat, mode: 'group' as const };
    return {
      printer: buildPresetField({ listing: data, slot: 'printer', filters: softFilters('printer'), pinned: refs.printer }),
      process: buildPresetField({ listing: data, slot: 'process', fit, filters: softFilters('process'), pinned: refs.process }),
      filament: buildPresetField({ listing: data, slot: 'filament', fit, filters: softFilters('filament'), pinned: refs.filament }),
    };
  }, [data, filters, printerName, compat, parentsBySource, refs.printer, refs.process, refs.filament]);
  const showAll = filters.turnOff;
  const pick = (slot: RowSlot, ref: PresetRef | null) => {
    setApplied(null);
    setUnresolved([]);
    onPick(slot, ref);
  };

  // A printer with one nozzle has nothing to choose; one that lost the chosen nozzle falls back to the first.
  useEffect(() => {
    if (info.data && !info.data.nozzles.some((nozzle) => nozzle.index === target.nozzle)) {
      onNozzle(info.data.nozzles[0]?.index ?? 1);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- only a new answer re-checks the nozzle
  }, [info.data]);

  const labels: Record<RowSlot, string> = {
    printer: t('fileManager.assemble.printerPreset'),
    process: t('fileManager.assemble.processPreset'),
    filament: t('fileManager.assemble.filamentPreset'),
  };
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
      <PipelineSelect
        onApply={(set) => {
          const out = onApplySet(set);
          setApplied({ name: out.name, changed: out.changed });
          setUnresolved(out.unresolved);
        }}
        applied={applied}
        unresolved={unresolved}
      />
      <div className="grid grid-cols-1 md:grid-cols-3 gap-2">
        {SLOTS.map((slot) => (
          <PresetSelect
            key={slot}
            label={labels[slot]}
            field={fields[slot]}
            value={refs[slot]}
            gone={gone[slot]}
            onShowAll={showAll}
            onChange={(ref) => pick(slot, ref)}
          />
        ))}
      </div>
      {nozzles.length > 1 && (
        <label className="flex items-center gap-2 text-xs text-bambu-gray">
          <span>{t('fileManager.assemble.nozzle')}</span>
          <Select
            value={String(target.nozzle)}
            onChange={(event) => onNozzle(Number(event.target.value))}
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

  // The family's catalogue, filters and choice state (spec slicing-setup §7.3).
  const catalogue = usePresetCatalogue();
  const filters = usePresetFilters({ source: true, farmModels: true, loaded: true });
  const choices = useChoiceFields<string>();
  const printerModels = useQuery({ queryKey: ['slicerPrinterModels'], queryFn: api.getPrinterModels });
  const models = useMemo(() => printerModels.data ?? {}, [printerModels.data]);
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
  const data = catalogue.data;
  const parentsBySource = useMemo(() => buildParentsBySource(data), [data]);
  const modelsSettled = printerModels.isFetched || printerModels.isError;
  const refsOf = (rowKey: string): Record<RowSlot, PresetRef | null> => ({
    printer: choices.get(fieldKey(rowKey, 'printer')).ref,
    process: choices.get(fieldKey(rowKey, 'process')).ref,
    filament: choices.get(fieldKey(rowKey, 'filament')).ref,
  });
  const goneOf = (rowKey: string): Record<RowSlot, boolean> => ({
    printer: choices.get(fieldKey(rowKey, 'printer')).gone,
    process: choices.get(fieldKey(rowKey, 'process')).gone,
    filament: choices.get(fieldKey(rowKey, 'filament')).gone,
  });
  const seedRow = (rowKey: string, d: UnifiedPresetsResponse) => {
    const picks = defaultsFor(d, null, models);
    for (const slot of SLOTS) choices.dispatch(fieldKey(rowKey, slot), { kind: 'reset', pick: picks[slot] }, POLICY[slot]);
  };

  // The first picks of a new form, or a re-layout's record (origin 'record'),
  // once both the form and the catalogue are in — whichever came first. A
  // record ref the listing lacks blocks its row until it returns or a person
  // picks (spec R6).
  const seenData = useRef<UnifiedPresetsResponse | undefined>(undefined);
  useEffect(() => {
    // wait for the printer-model registry too: without it nothing is known to suit the printer
    if (seeded || !data || !form || !modelsSettled) return;
    setSeeded(true);
    seenData.current = data;
    for (const target of form.targets) {
      if (!relayout) {
        seedRow(target.key, data);
        continue;
      }
      for (const slot of SLOTS) {
        const ref = target[slot];
        choices.dispatch(
          fieldKey(target.key, slot),
          { kind: 'seed', ref, origin: 'record', listed: ref !== null && findPreset(data, ref, slot) !== null },
          POLICY[slot],
        );
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seeded, data, form, relayout, modelsSettled]);

  // A new listing (a refresh): an automatic pick that left it is made again;
  // a chosen or recorded ref that left it blocks its row, and is restored when
  // it is listed again.
  useEffect(() => {
    if (!seeded || !data || !form || seenData.current === data) return;
    seenData.current = data;
    for (const target of form.targets) {
      const printer = refsOf(target.key).printer;
      const picks = () => defaultsFor(data, printer, models);
      choices.dispatch(
        fieldKey(target.key, 'printer'),
        {
          kind: 'refresh',
          listed: (r) => findPreset(data, r, 'printer') !== null,
          usable: () => true,
          repick: () => defaultsFor(data, null, models).printer,
        },
        POLICY.printer,
      );
      for (const slot of ['process', 'filament'] as const) {
        choices.dispatch(
          fieldKey(target.key, slot),
          {
            kind: 'refresh',
            listed: (r) => findPreset(data, r, slot) !== null,
            usable: usableFor(data, slot, printer, models),
            repick: () => picks()[slot],
          },
          POLICY[slot],
        );
      }
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seeded, data]);

  // A pick by hand. A new printer: the row's automatic process and filament
  // follow it, a process or filament chosen by hand stays (R6), the nozzle
  // starts over.
  const pickInRow = (rowKey: string, slot: RowSlot, ref: PresetRef | null) => {
    choices.dispatch(fieldKey(rowKey, slot), { kind: 'personPicked', ref }, POLICY[slot]);
    if (slot !== 'printer' || !data) return;
    const printer = choices.get(fieldKey(rowKey, 'printer')).ref;
    const picks = defaultsFor(data, printer, models);
    for (const s of ['process', 'filament'] as const) {
      choices.dispatch(
        fieldKey(rowKey, s),
        { kind: 'printerChanged', usable: usableFor(data, s, printer, models), repick: () => picks[s] },
        POLICY[s],
      );
    }
    update((current) => ({
      ...current,
      targets: current.targets.map((target) => (target.key === rowKey ? { ...target, nozzle: 1 } : target)),
    }));
  };
  // A saved set into ONE row: printer, process and slot 0's filament; its plate is not the row's (R11).
  const applySetInRow = (rowKey: string, set: SlicerPipeline) => {
    const changed: string[] = [];
    const unresolved: string[] = [];
    if (!data) return { name: set.name, changed, unresolved };
    const out = applyPipeline(set, data, { printer: true, process: true, plate: false, filaments: 'first' }, null, 1);
    const labels: Record<RowSlot, string> = {
      printer: t('fileManager.assemble.printerPreset'),
      process: t('fileManager.assemble.processPreset'),
      filament: t('fileManager.assemble.filamentPreset'),
    };
    const entries: [RowSlot, (typeof out)['printer']][] = [
      ['printer', out.printer],
      ['process', out.process],
      ['filament', out.filaments[0] ?? null],
    ];
    for (const [slot, entry] of entries) {
      if (entry === null) continue;
      if ('unresolved' in entry) {
        unresolved.push(labels[slot]);
        continue;
      }
      const before = choices.get(fieldKey(rowKey, slot)).ref;
      if (before?.source !== entry.ref.source || before?.id !== entry.ref.id) changed.push(labels[slot]);
      choices.dispatch(fieldKey(rowKey, slot), { kind: 'pipelineApplied', ref: entry.ref }, POLICY[slot]);
    }
    return { name: set.name, changed, unresolved };
  };
  const removeRow = (rowKey: string) => {
    for (const slot of SLOTS) choices.forget(fieldKey(rowKey, slot));
    update((current) => ({ ...current, targets: current.targets.filter((target) => target.key !== rowKey) }));
  };
  const addRow = () => {
    if (!data) return;
    const row = emptyTarget();
    update((current) => ({ ...current, targets: [...current.targets, row] }));
    seedRow(row.key, data);
  };
  const setNozzle = (rowKey: string, nozzle: number) =>
    update((current) => ({
      ...current,
      targets: current.targets.map((target) => (target.key === rowKey ? { ...target, nozzle } : target)),
    }));

  // What is validated and sent: the form with each row's current choices.
  const effectiveForm: AssembleForm | null = form
    ? { ...form, targets: form.targets.map((target) => ({ ...target, ...refsOf(target.key) })) }
    : null;
  const issues = effectiveForm ? validate(effectiveForm) : [];
  // the same queries the rows show their warning from: a printer preset that cannot be used blocks the button
  const printerInfos = useQueries({
    queries: (effectiveForm?.targets ?? []).map((target) => printerInfoQuery(target.printer)),
  });
  const printerUnusable =
    printerInfos.some((info) => info.isError) ||
    (form?.targets ?? []).some((target) => SLOTS.some((slot) => goneOf(target.key)[slot]));
  const submit = useMutation({
    mutationFn: () =>
      relayout
        ? api.relayoutFile(mode.fileId, toRelayoutRequest(effectiveForm as AssembleForm))
        : api.assembleProject(toRequest(effectiveForm as AssembleForm)),
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
      <Button variant="primary" onClick={onClose}>
        {t('common.close')}
      </Button>
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
              onClick={addRow}
              disabled={form.targets.length >= MAX_TARGETS}
            >
              <Plus className="w-4 h-4 mr-1" />
              {t('fileManager.assemble.addTarget')}
            </Button>
          </div>
          <p className="text-xs text-bambu-gray">{t('fileManager.assemble.targetsHint')}</p>
          {/* Once, above the rows (spec §7.3). */}
          <PresetListStatus catalogue={catalogue} />
          <PresetFilters filters={filters} parts={{ source: true, farmModels: true, loaded: true }} />
          {form.targets.map((target, index) => (
            <TargetEditor
              key={target.key}
              target={target}
              index={index}
              data={data}
              models={models}
              refs={refsOf(target.key)}
              gone={goneOf(target.key)}
              filters={filters}
              parentsBySource={parentsBySource}
              removable={form.targets.length > 1}
              onPick={(slot, ref) => pickInRow(target.key, slot, ref)}
              onApplySet={(set) => applySetInRow(target.key, set)}
              onNozzle={(nozzle) => setNozzle(target.key, nozzle)}
              onRemove={() => removeRow(target.key)}
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
