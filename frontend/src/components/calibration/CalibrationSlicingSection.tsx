// The calibration slicing section (spec slicing-setup r5 §7.2): one section
// for both calibration pages, composed of the slicing-setup family — the
// slicer cards (remembered per printer), the bed plate, saved sets (apply
// only), the preset-list status, the source segment and three preset selects
// in hide mode. It owns the choice state and answers the page through
// `onChange`; the page keeps everything K (the slot binding, the save) to
// itself.
//
// Preset mode (the page that prints): the printer must be PROVEN for the
// physical printer and the chosen nozzle (R4, D7), and the filament is picked
// by the slot's family (D6). Verify mode (slice for download): any printer,
// marked when it is another model; the first fitting filament.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import {
  api,
  type BedType,
  type PresetRef,
  type SlicerPipeline,
  type UnifiedPreset,
  type UnifiedPresetsResponse,
} from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import type { SlicerKind } from '../../hooks/useSlicerHealth';
import { buildParentsBySource, buildPrinterParents } from '../../utils/printerPresetParents';
import { TIER_ORDER, type Slot } from '../../utils/presetPickerUtils';
import { presetIsOfFamily } from '../../utils/sliceFarmFilter';
import {
  findPreset,
  pickFamilyFilament,
  pickPrinterFallback,
  pickProcessForField,
} from '../../utils/slicePresetPicker';
import {
  buildCompatibilityIndex,
  isPrinterPresetOfModel,
  isProvenPrinterPreset,
  presetCompatibility,
} from '../../utils/slicerPrinterMatch';
import { BedPlateSelect } from '../slicing-setup/BedPlateSelect';
import { PipelineSelect } from '../slicing-setup/PipelineSelect';
import { PresetFilters } from '../slicing-setup/PresetFilters';
import { PresetListStatus } from '../slicing-setup/PresetListStatus';
import { PresetSelect } from '../slicing-setup/PresetSelect';
import { SlicerChoice } from '../slicing-setup/SlicerChoice';
import { applyPipeline } from '../slicing-setup/applyPipeline';
import { POLICIES, useChoiceFields, type ChoiceEvent, type ChoicePolicy } from '../slicing-setup/choiceState';
import { buildPresetField } from '../slicing-setup/presetSets';
import { useBedPlate } from '../slicing-setup/useBedPlate';
import { usePresetCatalogue } from '../slicing-setup/usePresetCatalogue';
import { usePresetFilters } from '../slicing-setup/usePresetFilters';
import { useSlicerChoice } from '../slicing-setup/useSlicerChoice';

/** The loaded slot the filament is picked for; `key` = `${extruderId}:${amsId}:${slotId}`. */
export interface CalibrationSlot {
  key: string;
  label: string;
  familyId: string | null;
}

export interface CalibrationPresetChoice {
  slicer: SlicerKind | null;
  plate: BedType;
  printer: PresetRef | null;
  process: PresetRef | null;
  filament: PresetRef | null;
  /** Why Start / slice-for-download cannot run now. A pending family answer is
   *  NOT a reason (spec §7.2): it only withholds the automatic pick. */
  /** `loading`: the current context's catalogue has not arrived (or failed),
   *  or a chosen ref is not in it yet — nothing of an older catalogue is sent. */
  startBlocked: false | 'notProven' | 'empty' | 'gone' | 'noCatalogue' | 'loading';
  /** Said beside the filament field, with the slot's label. */
  familyHint: 'pending' | 'unavailable' | 'absent' | null;
}

interface Props {
  printerId: number;
  mode: 'preset' | 'verify';
  /** The page's method slices (not an Auto mode): only then is the farm read. */
  needsPresetPicker: boolean;
  nozzle: number;
  slot: CalibrationSlot | null;
  value: CalibrationPresetChoice;
  onChange: (v: CalibrationPresetChoice) => void;
  disabled?: boolean;
}

type FieldKey = 'printer' | 'process' | 'filament';
const POLICY: Record<FieldKey, ChoicePolicy> = {
  printer: POLICIES.calibrationPrinter,
  process: POLICIES.calibrationProcess,
  filament: POLICIES.calibrationFilament,
};

export function CalibrationSlicingSection({
  printerId,
  mode,
  needsPresetPicker,
  nozzle,
  slot,
  value,
  onChange,
  disabled,
}: Props) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const canSlice = hasPermission('library:upload');
  const preset = mode === 'preset';

  // Without library:upload there is no catalogue: no request, a sentence, and
  // Start is unavailable (spec §9).
  const catalogue = usePresetCatalogue({ enabled: canSlice });
  const data = catalogue.data;
  // The family facts come from the farm read, on the preset page of a slicing
  // method only — the read is enabled with every switch off.
  const filters = usePresetFilters({ source: true, farmRead: preset && needsPresetPicker ? 'always' : 'switches' });
  const slicer = useSlicerChoice(`bamdude:slicer-pick:calibration:${printerId}`);
  const bed = useBedPlate(slicer.effective);

  const printerQuery = useQuery({
    queryKey: ['printer', printerId],
    queryFn: () => api.getPrinter(printerId),
    staleTime: 60_000,
  });
  const physicalModel = printerQuery.data?.model ?? null;
  const printerModelsQuery = useQuery({
    queryKey: ['slicerPrinterModels'],
    queryFn: api.getPrinterModels,
    staleTime: Infinity,
  });
  const printerModels = useMemo(() => printerModelsQuery.data ?? {}, [printerModelsQuery.data]);

  const choices = useChoiceFields<FieldKey>();
  const printerRef = choices.get('printer').ref;
  const processRef = choices.get('process').ref;
  const filamentRef = choices.get('filament').ref;

  const parentsBySource = useMemo(() => buildParentsBySource(data), [data]);
  const selectedPrinter = data && printerRef ? findPreset(data, printerRef, 'printer') : null;
  const selectedPrinterName = selectedPrinter?.name ?? null;
  const printerParents = useMemo(() => buildPrinterParents(data, printerRef?.source ?? null), [data, printerRef?.source]);
  const compatIndex = useMemo(() => buildCompatibilityIndex(printerModels, printerParents), [printerModels, printerParents]);

  // R4: the proven match, one function for the list, Start and the sets.
  const proven = useCallback(
    (p: UnifiedPreset) =>
      physicalModel !== null &&
      isProvenPrinterPreset(p.name, parentsBySource[p.source] ?? null, { model: physicalModel, nozzle }, printerModels),
    [physicalModel, nozzle, parentsBySource, printerModels],
  );
  const printerProven = selectedPrinter !== null && proven(selectedPrinter);
  const otherModel =
    !preset &&
    selectedPrinter !== null &&
    physicalModel !== null &&
    isPrinterPresetOfModel(selectedPrinter.name, parentsBySource[selectedPrinter.source] ?? null, physicalModel, printerModels) === false;

  const printerSource = printerRef?.source ?? null;
  const softFilters = useMemo(
    () => filters.softFilters({ slot: 'printer', printerName: selectedPrinterName, printerSource, parentsBySource }),
    [filters, selectedPrinterName, printerSource, parentsBySource],
  );
  const fit = useMemo(
    () => ({ printerName: selectedPrinterName, compatIndex, mode: 'hide' as const }),
    [selectedPrinterName, compatIndex],
  );
  const printerField = useMemo(
    () =>
      data
        ? buildPresetField({
            listing: data,
            slot: 'printer',
            bound: preset ? { keep: proven, whenEmpty: 'showAll' } : undefined,
            filters: softFilters,
            pinned: printerRef,
          })
        : null,
    [data, preset, proven, softFilters, printerRef],
  );
  const processField = useMemo(
    () => (data ? buildPresetField({ listing: data, slot: 'process', fit, filters: softFilters, pinned: processRef }) : null),
    [data, fit, softFilters, processRef],
  );
  const filamentField = useMemo(
    () => (data ? buildPresetField({ listing: data, slot: 'filament', fit, filters: softFilters, pinned: filamentRef }) : null),
    [data, fit, softFilters, filamentRef],
  );

  // D6: the slot's family, once the farm has answered.
  const familyAnswered = filters.farmStatus === 'ready';
  const familyRefs = familyAnswered && slot?.familyId ? filters.farm?.families[slot.familyId] : undefined;

  // The automatic picks (R5).
  const mismatch = (p: UnifiedPreset, s: 'process' | 'filament') =>
    presetCompatibility(p, s, selectedPrinterName, compatIndex) === 'mismatch';
  const pickPrinter = (d: UnifiedPresetsResponse): PresetRef | null => {
    if (!preset) return pickPrinterFallback(d, buildParentsBySource(d));
    const first = TIER_ORDER.flatMap((tier) => d[tier].printer).find(proven);
    return first ? { source: first.source, id: first.id } : null;
  };
  const pickProcess = (d: UnifiedPresetsResponse): PresetRef | null =>
    pickProcessForField(d, processField?.pickKeep ?? (() => true), selectedPrinterName, compatIndex, preset ? { fittingOnly: true } : undefined);
  const pickFilament = (d: UnifiedPresetsResponse): PresetRef | null => {
    if (preset) return familyAnswered ? pickFamilyFilament(d, familyRefs, selectedPrinterName, compatIndex) : null;
    const all = TIER_ORDER.flatMap((tier) => d[tier].filament).filter((p) => !mismatch(p, 'filament'));
    const first = all.find((p) => filamentField?.pickKeep(p) ?? true) ?? all[0];
    return first ? { source: first.source, id: first.id } : null;
  };
  const listed = (d: UnifiedPresetsResponse, s: Slot) => (r: PresetRef) => findPreset(d, r, s) !== null;
  const usable = (d: UnifiedPresetsResponse, s: 'process' | 'filament') => (r: PresetRef) => {
    const p = findPreset(d, r, s);
    return p !== null && !mismatch(p, s);
  };

  // A person's choice that left the listing and was replaced, said beside the fields.
  const [goneNamed, setGoneNamed] = useState<FieldKey[]>([]);

  // The first pick: once the listing and (preset page) the physical model are in.
  const [seeded, setSeeded] = useState(false);
  const seenData = useRef<UnifiedPresetsResponse | undefined>(undefined);
  useEffect(() => {
    if (seeded || !data || (preset && physicalModel === null)) return;
    seenData.current = data;
    choices.dispatch('printer', { kind: 'reset', pick: pickPrinter(data) }, POLICY.printer);
    setSeeded(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seeded, data, physicalModel]);

  // The printer context: automatic process and filament picks follow.
  useEffect(() => {
    if (!seeded || !data) return;
    choices.dispatch('process', { kind: 'printerChanged', usable: usable(data, 'process'), repick: () => pickProcess(data) }, POLICY.process);
    choices.dispatch('filament', { kind: 'printerChanged', usable: usable(data, 'filament'), repick: () => pickFilament(data) }, POLICY.filament);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seeded, selectedPrinterName, compatIndex]);

  // A new listing: R6's refresh for every field.
  useEffect(() => {
    if (!seeded || !data || seenData.current === data) return;
    seenData.current = data;
    choices.dispatch('printer', { kind: 'refresh', listed: listed(data, 'printer'), usable: () => true, repick: () => pickPrinter(data) }, POLICY.printer);
    const named: FieldKey[] = [];
    for (const key of ['process', 'filament'] as const) {
      const event: ChoiceEvent = {
        kind: 'refresh',
        listed: listed(data, key),
        usable: usable(data, key),
        repick: () => (key === 'process' ? pickProcess(data) : pickFilament(data)),
      };
      if (choices.dispatch(key, event, POLICY[key]).namedGone) named.push(key);
    }
    if (named.length > 0) setGoneNamed((known) => [...known, ...named.filter((n) => !known.includes(n))]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seeded, data]);

  // The nozzle changed: a person's printer stays and is marked when not proven.
  const seenNozzle = useRef(nozzle);
  useEffect(() => {
    if (seenNozzle.current === nozzle) return;
    seenNozzle.current = nozzle;
    if (!seeded || !data) return;
    choices.dispatch(
      'printer',
      {
        kind: 'nozzleChanged',
        usable: (r) => !preset || (findPreset(data, r, 'printer') !== null && proven(findPreset(data, r, 'printer') as UnifiedPreset)),
        repick: () => pickPrinter(data),
      },
      POLICY.printer,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [nozzle]);

  // The source filter changed: automatic process and filament picks are made again.
  const seenSource = useRef(filters.source);
  useEffect(() => {
    if (seenSource.current === filters.source) return;
    seenSource.current = filters.source;
    if (!seeded || !data) return;
    choices.dispatch('process', { kind: 'filterChanged', repick: () => pickProcess(data) }, POLICY.process);
    choices.dispatch('filament', { kind: 'filterChanged', repick: () => pickFilament(data) }, POLICY.filament);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filters.source]);

  // D6: the family answer arrives, or arrives again — in whatever order it
  // comes with the catalogue and the slot. An empty automatic field is filled;
  // an automatic ref no longer of the slot's family is picked again; a
  // person's ref, and a person's clear, stay.
  const answer = preset && familyAnswered ? filters.farm : undefined;
  const seenAnswer = useRef<unknown>(undefined);
  useEffect(() => {
    // A lost answer (stale / failed read) is forgotten, so the next good one
    // counts even when TanStack hands back the same object for an equal body
    // (review I-3): a slot chosen meanwhile still gets its family pick.
    if (answer === undefined) {
      seenAnswer.current = undefined;
      return;
    }
    if (!seeded || !data || seenAnswer.current === answer) return;
    seenAnswer.current = answer;
    choices.dispatch(
      'filament',
      {
        kind: 'familyAnswered',
        repick: () => pickFilament(data),
        belongs: (r) => {
          const p = findPreset(data, r, 'filament');
          return p !== null && presetIsOfFamily(p, familyRefs);
        },
      },
      POLICY.filament,
    );
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seeded, data, answer]);

  // Another slot: a preset chosen for another spool is not carried over.
  const seenSlot = useRef(slot?.key ?? null);
  useEffect(() => {
    const key = slot?.key ?? null;
    if (seenSlot.current === key) return;
    seenSlot.current = key;
    if (!seeded || !data) return;
    choices.dispatch('filament', { kind: 'reset', pick: pickFilament(data) }, POLICY.filament);
    setGoneNamed((known) => known.filter((k) => k !== 'filament'));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [slot?.key]);

  const showAll = filters.turnOff;
  const pickByHand = (key: FieldKey, ref: PresetRef | null) => {
    setApplied(null);
    setUnresolved([]);
    setGoneNamed((known) => known.filter((k) => k !== key));
    choices.dispatch(key, { kind: 'personPicked', ref }, POLICY[key]);
  };

  // Saved sets (R11): the preset page lists only sets whose printer is proven
  // and fills printer, process and plate; the verify page also fills slot 0's
  // filament.
  const [applied, setApplied] = useState<{ name: string; changed: string[] } | null>(null);
  const [unresolved, setUnresolved] = useState<string[]>([]);
  const fieldLabel: Record<FieldKey, string> = {
    printer: t('slice.printer', 'Printer profile'),
    process: t('slice.process', 'Process profile'),
    filament: t('slice.filament', 'Filament profile'),
  };
  const applySet = (set: SlicerPipeline) => {
    if (!data) return;
    const out = applyPipeline(
      set,
      data,
      { printer: true, process: true, plate: false, filaments: preset ? 'none' : 'first' },
      slicer.effective,
      1,
    );
    const changed: string[] = [];
    const missing: string[] = [];
    const entries: [FieldKey, (typeof out)['printer']][] = [
      ['printer', out.printer],
      ['process', out.process],
      ['filament', out.filaments[0] ?? null],
    ];
    for (const [key, entry] of entries) {
      if (entry === null) continue;
      if ('unresolved' in entry) {
        missing.push(fieldLabel[key]);
        continue;
      }
      const before = choices.get(key).ref;
      if (before?.source !== entry.ref.source || before?.id !== entry.ref.id) changed.push(fieldLabel[key]);
      choices.dispatch(key, { kind: 'pipelineApplied', ref: entry.ref }, POLICY[key]);
    }
    if (bed.applyFromSet(set.bed_type).result === 'applied') changed.push(t('slice.bedType.label', 'Bed plate'));
    setGoneNamed([]);
    setApplied({ name: set.name, changed });
    setUnresolved(missing);
  };
  const setIsProven = (set: SlicerPipeline) => {
    const p = data ? findPreset(data, set.printer_preset, 'printer') : null;
    return p !== null && proven(p);
  };

  // What the page reads.
  const gone = (['printer', 'process', 'filament'] as const).some((k) => choices.get(k).gone);
  // A ref counts only when the CURRENT catalogue lists it: between a context
  // change and its answer the refs are an older catalogue's (review I-2).
  const unlisted = (['printer', 'process', 'filament'] as const).some((k) => {
    const ref = choices.get(k).ref;
    return ref !== null && (!data || findPreset(data, ref, k) === null);
  });
  const startBlocked: CalibrationPresetChoice['startBlocked'] = !canSlice
    ? 'noCatalogue'
    : !data || unlisted
      ? 'loading'
      : gone
        ? 'gone'
        : preset && printerRef !== null && !printerProven
          ? 'notProven'
          : printerRef === null || processRef === null || filamentRef === null
            ? 'empty'
            : false;
  let familyHint: CalibrationPresetChoice['familyHint'] = null;
  if (preset && needsPresetPicker && slot && filamentRef === null && canSlice) {
    const status = filters.farmStatus;
    if (status === 'pending') familyHint = 'pending';
    else if (status !== 'ready') familyHint = 'unavailable';
    else if (!familyRefs || (data && pickFamilyFilament(data, familyRefs, selectedPrinterName, compatIndex) === null)) familyHint = 'absent';
  }
  const choice: CalibrationPresetChoice = {
    slicer: slicer.effective,
    plate: bed.value,
    printer: printerRef,
    process: processRef,
    filament: filamentRef,
    startBlocked,
    familyHint,
  };
  const choiceKey = JSON.stringify(choice);
  useEffect(() => {
    if (JSON.stringify(value) !== choiceKey) onChange(JSON.parse(choiceKey) as CalibrationPresetChoice);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [choiceKey]);

  if (!canSlice) {
    return (
      <p role="status" className="text-sm text-amber-700 dark:text-amber-400">
        {t('slicingSetup.noCatalogue')}
      </p>
    );
  }

  return (
    <div className="space-y-3">
      <SlicerChoice value={slicer.effective} onChange={slicer.choose} disabled={disabled} />
      <BedPlateSelect value={bed.value} onChange={bed.choose} slicer={slicer.effective} note={bed.note} disabled={disabled} />
      <PipelineSelect
        onApply={applySet}
        filterSets={preset ? setIsProven : undefined}
        disabled={disabled}
        applied={applied}
        unresolved={unresolved}
      />
      <PresetListStatus catalogue={catalogue} disabled={disabled} />
      <PresetFilters filters={filters} parts={{ source: true }} disabled={disabled} />
      {goneNamed.length > 0 && (
        <p role="status" className="text-xs text-amber-700 dark:text-amber-400">
          {t('slice.chosenPresetGone', {
            fields: goneNamed.map((k) => fieldLabel[k]).join(', '),
            defaultValue: 'A profile you chose is no longer in the list, so another one was selected. Check: {{fields}}',
          })}
        </p>
      )}
      {data && printerField && processField && filamentField ? (
        <div className="grid grid-cols-1 gap-2">
          <PresetSelect
            label={fieldLabel.printer}
            field={printerField}
            value={printerRef}
            gone={choices.get('printer').gone}
            notProven={preset && printerRef !== null && !printerProven}
            otherModel={otherModel}
            onShowAll={showAll}
            onChange={(ref) => pickByHand('printer', ref)}
            disabled={disabled}
          />
          <PresetSelect
            label={fieldLabel.process}
            field={processField}
            value={processRef}
            onShowAll={showAll}
            onChange={(ref) => pickByHand('process', ref)}
            disabled={disabled}
          />
          <div>
            <PresetSelect
              label={fieldLabel.filament}
              field={filamentField}
              value={filamentRef}
              onShowAll={showAll}
              onChange={(ref) => pickByHand('filament', ref)}
              disabled={disabled}
            />
            {familyHint && slot && (
              <p role="status" className="mt-1 text-xs text-bambu-gray">
                {t(`slicingSetup.family.${familyHint}`, { slot: slot.label })}
              </p>
            )}
          </div>
        </div>
      ) : (
        <div className="p-3 bg-bambu-dark rounded text-sm text-bambu-gray">
          {t('filamentCali.verifyDownload.loadingPresets', 'Loading presets…')}
        </div>
      )}
    </div>
  );
}
