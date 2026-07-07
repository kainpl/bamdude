import { Cloud, CloudOff, Cog, Loader2, RefreshCw, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  api,
  type BedType,
  type PresetRef,
  type PresetSource,
  type SliceJobProgress,
  type SliceRequest,
  type SlicerCloudStatus,
  type UnifiedPreset,
  type UnifiedPresetsResponse,
} from '../api/client';
import { useSliceJobTracker } from '../contexts/SliceJobTrackerContext';
import { useToast } from '../contexts/ToastContext';
import { SlicePlateSelector } from './SlicePlateSelector';
import type { PlateFilament } from '../types/plates';
import { normalizeColorForCompare, colorsAreSimilar } from '../utils/amsHelpers';
import {
  PresetDropdown,
  PresetSourceControl,
} from './preset-picker/PresetTripletPicker';
import {
  TIER_ORDER as SLICE_MODAL_TIER_ORDER,
  matchesOwnerFilter,
  type OwnerFilter,
  type Slot,
} from './preset-picker/presetPickerUtils';
import { BedTypePicker } from './preset-picker/BedTypePicker';
import { SlicerPicker, type SlicerKind } from './preset-picker/SlicerPicker';
import {
  buildCompatibilityIndex,
  presetCompatibility,
  type PrinterCompatibilityIndex,
} from '../utils/slicerPrinterMatch';

export type SliceSource =
  | { kind: 'libraryFile'; id: number; filename: string }
  | { kind: 'archive'; id: number; filename: string };

interface SliceModalProps {
  source: SliceSource;
  onClose: () => void;
}

function pickDefault(by: UnifiedPresetsResponse, slot: Slot): PresetRef | null {
  for (const tier of SLICE_MODAL_TIER_ORDER) {
    const list = by[tier][slot];
    if (list.length > 0) {
      return { source: list[0].source, id: list[0].id };
    }
  }
  return null;
}

// Resolve a PresetRef back to its UnifiedPreset within the named slot, or
// null if it no longer resolves (e.g. the preset was deleted between the
// listing fetch and selection).
function findPreset(
  by: UnifiedPresetsResponse,
  ref: PresetRef | null,
  slot: Slot,
): UnifiedPreset | null {
  if (!ref) return null;
  return by[ref.source][slot].find((p) => p.id === ref.id) ?? null;
}

// Find a preset by exact name across tiers (local → cloud → standard). Used
// to honour the printer / process preset names a 3MF was prepared with.
function findPresetByName(
  by: UnifiedPresetsResponse,
  slot: Slot,
  name: string | null | undefined,
): PresetRef | null {
  if (!name) return null;
  for (const tier of SLICE_MODAL_TIER_ORDER) {
    const p = by[tier][slot].find((x) => x.name === name);
    if (p) return { source: p.source, id: p.id };
  }
  return null;
}

// Process default: honour the process preset the 3MF was prepared with
// (preferredName) when it's available and not incompatible with the selected
// printer; otherwise the first preset compatible with the printer in tier
// order, then the first whose compatibility is merely unknown, then plain
// priority. Keeps the pre-pick honest with both the embedded config and the
// printer filter instead of blindly taking list[0] (#1325).
function pickProcessDefault(
  by: UnifiedPresetsResponse,
  printerName: string | null,
  compatIndex: PrinterCompatibilityIndex,
  preferredName?: string | null,
): PresetRef | null {
  const preferred = findPresetByName(by, 'process', preferredName);
  if (preferred) {
    const p = findPreset(by, preferred, 'process');
    if (p && presetCompatibility(p, 'process', printerName, compatIndex) !== 'mismatch') {
      return preferred;
    }
  }
  for (const wanted of ['match', 'unknown'] as const) {
    for (const tier of SLICE_MODAL_TIER_ORDER) {
      for (const p of by[tier].process) {
        if (presetCompatibility(p, 'process', printerName, compatIndex) === wanted) {
          return { source: p.source, id: p.id };
        }
      }
    }
  }
  return pickDefault(by, 'process');
}

const TIER_BONUS: Record<PresetSource, number> = {
  local: 1.75,
  orca_cloud: 1.5,
  cloud: 1.0,
  standard: 0.5,
};

function pickFilamentForSlot(
  by: UnifiedPresetsResponse,
  required: { type: string; color: string },
  printerName: string | null,
  compatIndex: PrinterCompatibilityIndex,
): PresetRef | null {
  // Score every filament preset against the plate slot's required (type,
  // colour) and pick the highest. Mirrors the AMS slot-mapping match in the
  // print/schedule modal: type match dominates, exact-colour-match bumps over
  // similar-colour-match, and a small per-tier bonus breaks ties so cloud
  // user customisations win over standard bundled fallbacks of equal merit.
  const reqType = required.type.trim().toUpperCase();
  const reqColor = normalizeColorForCompare(required.color);

  let best: { ref: PresetRef; score: number } | null = null;
  for (const tier of SLICE_MODAL_TIER_ORDER) {
    for (const p of by[tier].filament) {
      let score = 0;
      const presetType = (p.filament_type ?? '').trim().toUpperCase();
      const presetColor = normalizeColorForCompare(p.filament_colour ?? '');
      if (reqType && presetType && reqType === presetType) score += 10;
      if (reqColor && presetColor) {
        if (presetColor === reqColor) score += 5;
        else if (colorsAreSimilar(p.filament_colour ?? '', required.color)) score += 2;
      }
      score += TIER_BONUS[tier];
      // Demote printer-incompatible filaments (#1325): a penalty rather than a
      // hard skip so the pick still degrades gracefully if every filament
      // mismatches the selected printer.
      if (presetCompatibility(p, 'filament', printerName, compatIndex) === 'mismatch') {
        score -= 100;
      }
      if (best == null || score > best.score) {
        best = { ref: { source: p.source, id: p.id }, score };
      }
    }
  }
  // Fall back to plain priority pick if every preset scored 0+tier (i.e. no
  // metadata matched). The fallback is exactly the single-color default —
  // first preset in the highest-priority non-empty tier.
  if (best == null) return pickDefault(by, 'filament');
  return best.ref;
}

// Inline spinner for the filament-requirements query. The backend runs a
// preview slice on first open of an unsliced project file (cached after);
// on a complex multi-color model that's a real slice — multi-second to
// multi-minute. The spinner shows elapsed seconds, polls the sidecar's
// --pipe progress (via /slicer/preview-progress proxy) for live stage +
// percent, and after ~5s surfaces a "this is a one-time slice — repeat
// opens are instant" note so users don't worry it'll be slow forever.
//
// requestId: a UUID generated by the modal when the filament-requirements
// fetch starts. Forwarded to the sidecar via the API call AND used here
// to poll the matching progress snapshot. Same id, two consumers.
function FilamentAnalysisSpinner({
  requestId,
  sourceName,
}: {
  requestId: string;
  sourceName: string;
}) {
  const { t } = useTranslation();
  const { showPersistentToast, dismissToast } = useToast();
  const [elapsed, setElapsed] = useState(0);
  const [progress, setProgress] = useState<SliceJobProgress | null>(null);
  // Defensive decode — see prettifyFilename comment in SliceJobTrackerContext.
  let prettyName = sourceName;
  try {
    prettyName = decodeURIComponent(sourceName);
  } catch {
    /* keep raw on malformed encoding */
  }

  // Elapsed-time tick.
  useEffect(() => {
    const startedAt = Date.now();
    const id = setInterval(() => setElapsed(Math.floor((Date.now() - startedAt) / 1000)), 1000);
    return () => clearInterval(id);
  }, []);

  // Progress polling — once per second while the spinner is mounted.
  // Mirrors the slice-job tracker's cadence. Sidecar 404s during the
  // race window between fetch start and progressStore.start() are
  // swallowed by the API method (returns null) so we keep polling.
  useEffect(() => {
    let cancelled = false;
    const id = setInterval(async () => {
      if (cancelled) return;
      const snap = await api.getPreviewSliceProgress(requestId);
      if (!cancelled && snap) setProgress(snap);
    }, 1000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [requestId]);

  // Mirror the spinner's contents into a persistent toast so the user
  // sees activity even when their cursor is elsewhere on the page.
  // Dismissed in the parent's effect when the requirements arrive.
  const toastId = `slice-preview-${requestId}`;
  useEffect(() => {
    const hasUseful = progress && progress.stage && progress.total_percent > 0;
    const elapsedStr = formatElapsed(elapsed);
    if (hasUseful) {
      showPersistentToast(
        toastId,
        t(
          'slice.previewWithProgress',
          'Analyzing {{name}} — {{stage}} ({{percent}}%) — {{elapsed}}',
          {
            name: prettyName,
            stage: progress!.stage,
            percent: Math.min(100, Math.max(0, Math.round(progress!.total_percent))),
            elapsed: elapsedStr,
          },
        ),
        'loading',
      );
    } else {
      showPersistentToast(
        toastId,
        t('slice.previewToast', 'Analyzing {{name}} — {{elapsed}}', {
          name: prettyName,
          elapsed: elapsedStr,
        }),
        'loading',
      );
    }
    return () => {
      dismissToast(toastId);
    };
  }, [elapsed, progress, prettyName, showPersistentToast, dismissToast, t, toastId]);

  const stage = progress?.stage;
  const percent = progress?.total_percent;
  const inlineLabel =
    stage && typeof percent === 'number' && percent > 0
      ? `${stage} (${Math.min(100, Math.max(0, Math.round(percent)))}%)`
      : t('slice.analyzingPlateFilaments', 'Analyzing plate filaments…');
  return (
    <div className="flex flex-col gap-1 text-bambu-gray text-sm py-2">
      <div className="flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin" />
        {inlineLabel}
        <span className="text-xs tabular-nums">{elapsed}s</span>
      </div>
      {elapsed >= 5 && (
        <div className="text-xs text-bambu-gray/70 pl-6">
          {t(
            'slice.analyzingPlateFilamentsHint',
            'Running a preview slice to discover which AMS slots this plate uses. Cached after — re-opening is instant.',
          )}
        </div>
      )}
    </div>
  );
}

function formatElapsed(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const remS = s % 60;
  if (m < 60) return `${m}m ${remS}s`;
  const h = Math.floor(m / 60);
  const remM = m % 60;
  return `${h}h ${remM}m`;
}

export function SliceModal({ source, onClose }: SliceModalProps) {
  const { t } = useTranslation();
  const { trackJob } = useSliceJobTracker();

  const [printerPreset, setPrinterPreset] = useState<PresetRef | null>(null);
  const [processPreset, setProcessPreset] = useState<PresetRef | null>(null);
  // One filament ref per plate slot, in plate order. For STL / single-plate /
  // single-color sources this is a one-element array; multi-color 3MFs get one
  // entry per AMS slot the plate uses. Pre-pick (effect below) initialises
  // each slot from the source plate's required (type, colour).
  const [filamentPresets, setFilamentPresets] = useState<(PresetRef | null)[]>([]);
  const [errorMessage, setErrorMessage] = useState<string | null>(null);
  // Owner filter for the preset dropdowns — same 3-state model as the
  // ProfilesPage filter (``all`` / ``custom`` ("My presets") /
  // ``builtin``). Persisted in localStorage so a user who always picks
  // their own profiles doesn't have to flip the toggle on every modal
  // open. Default ``all`` keeps the existing behaviour for first-time
  // users / fresh browsers.
  const [filterOwner, setFilterOwner] = useState<OwnerFilter>(() => {
    if (typeof localStorage === 'undefined') return 'all';
    try {
      const stored = localStorage.getItem('bamdude:slice-modal:filter-owner');
      return stored === 'custom' || stored === 'builtin' ? stored : 'all';
    } catch {
      return 'all';
    }
  });
  useEffect(() => {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem('bamdude:slice-modal:filter-owner', filterOwner);
    } catch {
      /* private mode / quota — silently skip */
    }
  }, [filterOwner]);

  // Bed plate override sent to the slicer as ``--curr-bed-type``. Default
  // ``Textured PEI Plate`` matches the factory plate shipped with X1C / P1S
  // / H2D — the modern Bambu lineup the majority of our users own. A1 /
  // A1-mini owners who run Smooth PEI (SuperTack) will flip this once and
  // localStorage persists the pick, so subsequent slices land on the right
  // adhesion temps without re-clicking. Without this override the slicer
  // CLI defaults to "Cool Plate", which produces the wrong first-layer bed
  // temperature for any X1/A1 user with a default plate.
  const [bedType, setBedType] = useState<BedType>(() => {
    if (typeof localStorage === 'undefined') return 'Textured PEI Plate';
    try {
      const stored = localStorage.getItem('bamdude:slice-modal:bed-type');
      const allowed: BedType[] = [
        'Cool Plate',
        'Engineering Plate',
        'High Temp Plate',
        'Textured PEI Plate',
        'Supertack Plate',
      ];
      return (allowed as string[]).includes(stored ?? '') ? (stored as BedType) : 'Textured PEI Plate';
    } catch {
      return 'Textured PEI Plate';
    }
  });
  useEffect(() => {
    if (typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem('bamdude:slice-modal:bed-type', bedType);
    } catch {
      /* private mode / quota — silently skip */
    }
  }, [bedType]);
  // When the owner filter narrows the visible set, any current selection
  // that no longer matches must drop to null — otherwise the dropdown
  // would render an out-of-section value the user can no longer see in
  // the list, and clicking Slice would submit a preset they didn't
  // (re-)pick under the new filter. Pre-pick effect won't re-fire because
  // its dep is presetsQuery.data only.
  // null = plate not yet picked (or single-plate / non-3MF — picker is skipped
  // and we'll backfill 1 at submit time). Set to a 1-indexed plate number once
  // the user picks one (or implicitly for single-plate sources).
  const [selectedPlate, setSelectedPlate] = useState<number | null>(null);
  // "Slice all plates": sends ``plate=0`` so the backend slices every plate
  // (one archive, one multi-plate 3MF) instead of a single picked plate.
  // Filament selection then covers every project slot (used_in_plate forced
  // true below) since across the whole project every defined slot is used.
  const [sliceAllPlates, setSliceAllPlates] = useState(false);

  // Per-job slicer picker (B.4 follow-up). Visible only when both sidecars
  // are reachable — otherwise the global preferred_slicer setting is the
  // sole sensible target and the picker would just be misleading. The pick
  // is persisted per (source kind, source id) in localStorage so re-slicing
  // the same file defaults to the user's last choice; first-time defaults
  // to the global preferred_slicer.
  const settingsQuery = useQuery({
    queryKey: ['settings'],
    queryFn: api.getSettings,
    staleTime: 60_000,
  });
  // Backend caches the health response per (kind, url) for 30 s — these
  // two queries hit the network at most once per modal open, even if the
  // SliceModal re-mounts a few times during the user's flow.
  const orcaHealthQuery = useQuery({
    queryKey: ['slicerHealth', 'orcaslicer'],
    queryFn: () => api.getSlicerHealth('orcaslicer'),
    staleTime: 30_000,
    retry: false,
  });
  const bambuHealthQuery = useQuery({
    queryKey: ['slicerHealth', 'bambu_studio'],
    queryFn: () => api.getSlicerHealth('bambu_studio'),
    staleTime: 30_000,
    retry: false,
  });
  const orcaHealthy = orcaHealthQuery.data?.healthy === true;
  const bambuHealthy = bambuHealthQuery.data?.healthy === true;
  const showSlicerPicker = orcaHealthy && bambuHealthy;

  const slicerPickStorageKey = `bamdude:slicer-pick:${source.kind}:${source.id}`;
  const [pickedSlicer, setPickedSlicer] = useState<SlicerKind | null>(() => {
    if (typeof localStorage === 'undefined') return null;
    try {
      const stored = localStorage.getItem(slicerPickStorageKey);
      return stored === 'orcaslicer' || stored === 'bambu_studio' ? stored : null;
    } catch {
      return null;
    }
  });
  // Auto-select / default pick logic. Two cases beyond "user already picked":
  //
  //   1. Only one sidecar is healthy → lock to that one. Stops the modal
  //      from defaulting to a global preferred_slicer that's offline,
  //      and visually reflects "you have one option" in the picker cards.
  //
  //   2. Both healthy → first-time default to the global preferred_slicer
  //      (same as before). Subsequent renders keep whatever the user picked
  //      (persisted via localStorage on a separate effect).
  //
  // Also: if the user previously picked a sidecar that is now offline,
  // override to the healthy one. Otherwise the Slice button would silently
  // submit an offline-targeted request and fail at backend.
  useEffect(() => {
    if (orcaHealthQuery.isLoading || bambuHealthQuery.isLoading) return;
    const onlyOrca = orcaHealthy && !bambuHealthy;
    const onlyBambu = bambuHealthy && !orcaHealthy;
    if (onlyOrca) {
      if (pickedSlicer !== 'orcaslicer') setPickedSlicer('orcaslicer');
      return;
    }
    if (onlyBambu) {
      if (pickedSlicer !== 'bambu_studio') setPickedSlicer('bambu_studio');
      return;
    }
    if (!showSlicerPicker) return; // neither healthy — leave as-is
    if (pickedSlicer != null) return;
    const preferred = settingsQuery.data?.preferred_slicer;
    if (preferred === 'orcaslicer' || preferred === 'bambu_studio') {
      setPickedSlicer(preferred);
    } else {
      setPickedSlicer('bambu_studio');
    }
  }, [
    showSlicerPicker,
    orcaHealthy,
    bambuHealthy,
    orcaHealthQuery.isLoading,
    bambuHealthQuery.isLoading,
    pickedSlicer,
    settingsQuery.data?.preferred_slicer,
  ]);
  // Persist user's pick across modal opens.
  useEffect(() => {
    if (pickedSlicer == null || typeof localStorage === 'undefined') return;
    try {
      localStorage.setItem(slicerPickStorageKey, pickedSlicer);
    } catch {
      /* private mode / quota — silently skip */
    }
  }, [pickedSlicer, slicerPickStorageKey]);

  const platesQuery = useQuery({
    queryKey: ['slicePlates', source.kind, source.id],
    queryFn: async () => {
      if (source.kind === 'libraryFile') {
        return api.getLibraryFilePlates(source.id);
      }
      return api.getArchivePlates(source.id);
    },
    staleTime: 60_000,
  });

  const isMultiPlate =
    !!platesQuery.data?.is_multi_plate && (platesQuery.data?.plates?.length ?? 0) > 1;
  // Single-plate / non-3MF / fetch failure: skip the picker, default to plate 1
  // at submit time so the backend's existing default behaviour is preserved.
  // Multi-plate: auto-pick the first plate on load so the filament-reqs +
  // presets queries fire immediately (the inline ``<SlicePlateSelector>``
  // lets the user reassign without blocking the rest of the modal).
  const needsPlatePicker = isMultiPlate && selectedPlate == null;
  useEffect(() => {
    if (!isMultiPlate || selectedPlate != null) return;
    const first = platesQuery.data?.plates?.[0]?.index;
    if (first != null) setSelectedPlate(first);
  }, [isMultiPlate, selectedPlate, platesQuery.data]);

  // Per-plate filament requirements via the same endpoint the print/schedule
  // modal uses. Reusing it here keeps the SliceModal honest with whatever
  // logic that endpoint applies (slice_info parsing, preview-slice fallback,
  // future enhancements for unsliced project files, dual-nozzle fields, etc.)
  // instead of duplicating extraction. plate_id is always sent: single-plate
  // falls through to plate 1 server-side; multi-plate uses the user's pick.
  const effectivePlateId = selectedPlate ?? 1;
  // Generate a request_id per (source, plate) pair so the backend's
  // preview-slice and the FilamentAnalysisSpinner's progress poll share the
  // same id. useMemo keeps it stable across renders within the same pair;
  // switching plates regenerates so a stale poll doesn't bleed progress
  // between plates.
  const previewRequestId = useMemo(() => {
    const random =
      typeof crypto !== 'undefined' && 'randomUUID' in crypto
        ? crypto.randomUUID()
        : `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    // Tag the id with the (source, plate) so logs/Network panel show which
    // pair owns the poll. Also lets the lint rule see the deps in use.
    return `${source.kind}-${source.id}-p${effectivePlateId}-${random}`;
  }, [source.kind, source.id, effectivePlateId]);
  const filamentReqsQuery = useQuery({
    queryKey: ['sliceFilamentReqs', source.kind, source.id, effectivePlateId],
    queryFn: async () => {
      if (source.kind === 'libraryFile') {
        return api.getLibraryFileFilamentRequirements(source.id, effectivePlateId, previewRequestId);
      }
      return api.getArchiveFilamentRequirements(source.id, effectivePlateId, previewRequestId);
    },
    enabled: !needsPlatePicker,
    staleTime: 60_000,
  });

  // Filament slot list for the active plate. Falls back to one synthetic slot
  // for STL/STEP and any "no metadata available" case so the modal still
  // works (single dropdown, mono-color slice).
  const filamentSlots = useMemo<PlateFilament[]>(() => {
    const reqs = filamentReqsQuery.data?.filaments ?? [];
    const base: PlateFilament[] =
      reqs.length > 0 ? (reqs as PlateFilament[]) : [{ slot_id: 1, type: '', color: '', used_grams: 0, used_meters: 0 }];
    // In slice-all mode every defined slot is used by at least one plate, so
    // drop the per-plate "not used" gating that disables the dropdowns.
    if (sliceAllPlates) {
      return base.map((slot) => ({ ...slot, used_in_plate: true }));
    }
    return base;
  }, [sliceAllPlates, filamentReqsQuery.data]);

  const queryClient = useQueryClient();
  const presetsQuery = useQuery({
    queryKey: ['slicerPresets'],
    queryFn: () => api.getSlicerPresets(),
    staleTime: 60_000,
    // Don't fetch presets while the plate picker is on screen — saves a
    // round-trip if the user cancels out of the plate step.
    enabled: !platesQuery.isLoading && !needsPlatePicker,
  });

  // Manual refresh — bypasses the backend's cloud + bundled preset caches for
  // one call so a user who deleted a preset in Bambu Studio / Handy sees the
  // change immediately (#1581). The cache write inside the backend fetchers
  // refills with the fresh result, so later normal callers stay cached.
  const [isRefreshing, setIsRefreshing] = useState(false);
  const handleRefreshPresets = async () => {
    if (isRefreshing) return;
    setIsRefreshing(true);
    try {
      const fresh = await api.getSlicerPresets({ refresh: true });
      queryClient.setQueryData(['slicerPresets'], fresh);
    } catch {
      // Fall through to invalidate so React Query retries on next render,
      // surfacing the failure through the existing presetsQuery error path.
      queryClient.invalidateQueries({ queryKey: ['slicerPresets'] });
    } finally {
      setIsRefreshing(false);
    }
  };

  // Canonical Bambu printer-model registry — drives the @BBL name fallback in
  // the compatibility matcher for cloud / standard presets (#1325). Long
  // staleTime: the registry only changes across backend releases.
  const printerModelsQuery = useQuery({
    queryKey: ['slicerPrinterModels'],
    queryFn: api.getPrinterModels,
    staleTime: Infinity,
  });

  // Selected-printer context for the process / filament filter (#1325).
  const selectedPrinterName = useMemo<string | null>(() => {
    if (!presetsQuery.data || !printerPreset) return null;
    return findPreset(presetsQuery.data, printerPreset, 'printer')?.name ?? null;
  }, [presetsQuery.data, printerPreset]);
  // Compatibility ground truth: the slicer's own `compatible_printers` list
  // on local-imported presets, plus the @BBL <code> name fallback for cloud /
  // standard presets via the backend Bambu printer-model registry.
  const compatIndex = useMemo<PrinterCompatibilityIndex>(
    () => buildCompatibilityIndex(printerModelsQuery.data ?? {}),
    [printerModelsQuery.data],
  );

  // Printer / process preset names the source 3MF was prepared with. The
  // plates query resolves before the presets query (the latter is gated on
  // it), so these are known by the time the pre-pick effects run.
  const embeddedPrinter = platesQuery.data?.embedded_printer ?? null;
  const embeddedProcess = platesQuery.data?.embedded_process ?? null;

  // Printer pre-pick: defaults to the printer the 3MF was prepared for when
  // that preset is available, else the first listed printer (see
  // SLICE_MODAL_TIER_ORDER). Runs once when presets first arrive; later
  // re-renders preserve any manual choice (#1325).
  useEffect(() => {
    const data = presetsQuery.data;
    if (!data) return;
    if (printerPreset == null) {
      setPrinterPreset(
        findPresetByName(data, 'printer', embeddedPrinter) ?? pickDefault(data, 'printer'),
      );
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [presetsQuery.data, embeddedPrinter]);

  // Process pre-pick / re-pick (#1325): defaults to a process compatible with
  // the selected printer (honouring the 3MF's embedded process first), and
  // re-defaults when a printer change leaves the current process incompatible.
  // A compatible or unknown manual pick is kept.
  useEffect(() => {
    const data = presetsQuery.data;
    if (!data) return;
    setProcessPreset((current) => {
      if (current) {
        const p = findPreset(data, current, 'process');
        if (p && presetCompatibility(p, 'process', selectedPrinterName, compatIndex) !== 'mismatch') {
          return current;
        }
      }
      return pickProcessDefault(data, selectedPrinterName, compatIndex, embeddedProcess);
    });
  }, [presetsQuery.data, selectedPrinterName, compatIndex, embeddedProcess]);

  // Filament pre-pick: re-runs when the active filament-slot count changes
  // (plate selection, single-plate metadata arriving) or the selected printer
  // changes. Each slot scores every available filament preset against the
  // slot's required (type, colour); an existing pick (incl. a user override)
  // is kept as long as it's still compatible with the selected printer, while
  // null slots and printer-incompatible picks are re-picked (#1325).
  useEffect(() => {
    const data = presetsQuery.data;
    if (!data) return;
    setFilamentPresets((current) => {
      return filamentSlots.map((slot, i) => {
        const cur = current[i] ?? null;
        if (cur) {
          const p = findPreset(data, cur, 'filament');
          if (p && presetCompatibility(p, 'filament', selectedPrinterName, compatIndex) !== 'mismatch') {
            return cur;
          }
        }
        return pickFilamentForSlot(
          data,
          { type: slot.type, color: slot.color },
          selectedPrinterName,
          compatIndex,
        );
      });
    });
  }, [presetsQuery.data, filamentSlots, selectedPrinterName, compatIndex]);

  // Clear any current preset selection that no longer satisfies the
  // owner filter (resolves the picked id against the loaded preset
  // catalogue and drops the value if the resolved entry — or its absence
  // — fails ``matchesOwnerFilter``). The pre-pick effects above won't
  // re-fire because their dep is presetsQuery.data, so the dropdowns
  // surface the placeholder option until the user re-picks under the
  // narrower filter.
  useEffect(() => {
    if (!presetsQuery.data) return;
    const data = presetsQuery.data;
    const lookup = (slot: Slot, ref: PresetRef | null): UnifiedPreset | null => {
      if (!ref) return null;
      return data[ref.source][slot].find((p) => p.id === ref.id) ?? null;
    };
    const dropIfStale = (slot: Slot, ref: PresetRef | null): PresetRef | null => {
      const found = lookup(slot, ref);
      if (!found) return ref;
      return matchesOwnerFilter(found, filterOwner) ? ref : null;
    };
    setPrinterPreset((cur) => dropIfStale('printer', cur));
    setProcessPreset((cur) => dropIfStale('process', cur));
    setFilamentPresets((cur) => cur.map((ref) => dropIfStale('filament', ref)));
  }, [filterOwner, presetsQuery.data]);

  const enqueueMutation = useMutation({
    mutationFn: async () => {
      if (
        !printerPreset ||
        !processPreset ||
        filamentPresets.length === 0 ||
        filamentPresets.some((r) => r == null)
      ) {
        throw new Error(t('slice.allPresetsRequired', 'All presets must be selected'));
      }
      const body: SliceRequest = {
        printer_preset: printerPreset,
        process_preset: processPreset,
        // The first slot also goes into the legacy singular field so the
        // backend's older callers / clients keep behaving the same — the
        // backend validator prefers `filament_presets` when both are set.
        filament_preset: filamentPresets[0] as PresetRef,
        filament_presets: filamentPresets as PresetRef[],
        // Always send a concrete plate number when the source is multi-plate;
        // omit otherwise so the backend default applies for STL / single-plate
        // 3MF sources where the concept doesn't apply.
        ...(sliceAllPlates ? { plate: 0 } : selectedPlate != null ? { plate: selectedPlate } : {}),
        // Per-job slicer override. Only sent when the picker is actually
        // visible to the user (both sidecars healthy) AND the user picked
        // something — otherwise the global preferred_slicer setting decides.
        ...(pickedSlicer != null ? { slicer: pickedSlicer } : {}),
        // Bed plate override → sidecar's ``bedType`` form field →
        // ``--curr-bed-type`` CLI flag.
        bed_type: bedType,
      };
      if (source.kind === 'libraryFile') {
        return api.sliceLibraryFile(source.id, body);
      }
      return api.sliceArchive(source.id, body);
    },
    onSuccess: (enqueue) => {
      trackJob(enqueue.job_id, source.kind, source.filename);
      onClose();
    },
    onError: (err: unknown) => {
      const msg = err instanceof Error ? err.message : String(err);
      setErrorMessage(msg);
    },
  });

  // Cross-printer re-slicing is supported: the slicer CLI re-slices a 3MF
  // for whatever printer the chosen bundle/preset names — bed, kinematics,
  // nozzle count and start-gcode all come from the target. No source/target
  // model gate (#1325 cross-printer; upstream Bambuddy ed27b27a).

  // Slice button gates only on a complete picker selection. Multi-plate
  // sources also need an explicit plate pick so the slicer (and the
  // filament-reqs query that feeds the dropdowns) target the right plate.
  // Also gates on the preview-slice / embedded-metadata read having
  // succeeded (``filamentReqsQuery.isSuccess``) — until then, the synthetic
  // single-slot fallback would have auto-enabled the button on opaque
  // defaults before the slicer returned the real slot map.
  // Upstream Bambuddy commit a64cfbbd.
  const isReady =
    printerPreset != null &&
    processPreset != null &&
    filamentReqsQuery.isSuccess &&
    filamentPresets.length > 0 &&
    filamentPresets.every((r) => r != null) &&
    (!isMultiPlate || sliceAllPlates || selectedPlate != null);
  const isEnqueuing = enqueueMutation.isPending;

  // Slicer Pipelines (#1425) — apply a saved preset bundle to all four slots
  // with one pick, or save the current selection as a new pipeline. Managed in
  // Settings → Pipelines.
  const { showToast } = useToast();
  const pipelinesQuery = useQuery({
    queryKey: ['slicer-pipelines'],
    queryFn: () => api.listSlicerPipelines(),
    staleTime: 60_000,
  });
  const [savePipelineOpen, setSavePipelineOpen] = useState(false);
  const [pipelineDraftName, setPipelineDraftName] = useState('');
  const createPipelineMutation = useMutation({
    mutationFn: (body: {
      name: string;
      printer_preset: PresetRef;
      process_preset: PresetRef;
      filament_presets: PresetRef[];
      bed_type: string | null;
    }) => api.createSlicerPipeline(body),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slicer-pipelines'] });
      showToast(t('slice.pipelines.toast.saved', 'Pipeline saved'), 'success');
      setSavePipelineOpen(false);
      setPipelineDraftName('');
    },
    onError: (err: Error) => {
      showToast(err.message || t('slice.pipelines.toast.saveFailed', 'Save failed'), 'error');
    },
  });

  // Single-screen layout: preset picker
  // picker. While the plates query is in-flight we still render the shell
  // because the presets query is gated on it; the loader covers both.
  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4"
      onClick={() => {
        if (!isEnqueuing) onClose();
      }}
    >
      <div
        className="w-full max-w-xl max-h-[85vh] flex flex-col rounded-lg bg-bambu-dark-secondary border border-bambu-dark-tertiary/60"
        onClick={(e) => e.stopPropagation()}
      >
        {/* Header */}
        <div className="flex-shrink-0 flex items-start justify-between gap-3 px-4 pt-4 pb-3 border-b border-bambu-dark-tertiary/40">
          <div className="min-w-0">
            <h3 className="text-white font-medium flex items-center gap-2">
              <Cog className="w-4 h-4" />
              {t('slice.title', 'Slice model')}
            </h3>
            <p className="text-xs text-bambu-gray mt-1 truncate" title={source.filename}>
              {source.filename}
              {selectedPlate != null
                ? ` • ${t('archives.platePicker.plateLabel', { index: selectedPlate })}`
                : ''}
            </p>
          </div>
          <button
            onClick={onClose}
            disabled={isEnqueuing}
            className="flex-shrink-0 text-bambu-gray hover:text-white transition-colors disabled:opacity-50"
            aria-label={t('common.close', 'Close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {/* Inline plate picker for multi-plate sources. Visually mirrors
              ``PrintModal/PlateSelector`` so the slice + print flows feel
              consistent (vertical paginator strip + big details card).
              Renders above the other sections so the user sees their
              plate context first; switching plates re-keys the
              filament-reqs query so the dropdowns below realign to the
              new plate's required (type, color) automatically. */}
          {isMultiPlate && platesQuery.data && (
            <>
              <label className="flex items-center gap-2 text-sm text-white cursor-pointer">
                <input
                  type="checkbox"
                  checked={sliceAllPlates}
                  onChange={(e) => setSliceAllPlates(e.target.checked)}
                  disabled={isEnqueuing}
                  className="accent-bambu-green"
                />
                {t('slice.allPlates', 'Slice all plates')}
              </label>
              {!sliceAllPlates && (
                <SlicePlateSelector
                  plates={platesQuery.data.plates}
                  selectedPlate={selectedPlate}
                  onSelect={setSelectedPlate}
                  disabled={isEnqueuing}
                />
              )}
            </>
          )}
          {/* Preset listing loader — printer/process dropdowns can't render
              without it. Plate query reuses the same spinner since it's
              also blocking. */}
          {(platesQuery.isLoading || presetsQuery.isLoading) && (
            <div className="flex items-center gap-2 text-bambu-gray text-sm">
              <Loader2 className="w-4 h-4 animate-spin" />
              {t('slice.loadingPresets', 'Loading presets…')}
            </div>
          )}

          {presetsQuery.isError && (
            <div className="text-sm text-red-400" role="alert">
              {t(
                'slice.presetsLoadFailed',
                'Failed to load presets. Open Settings → Profiles to import them, or sign in to Bambu Cloud.',
              )}
            </div>
          )}

          {presetsQuery.data && (
            <>
              <div className="flex items-start justify-between gap-2">
                <div className="flex-1 space-y-2">
                  <CloudStatusBanner status={presetsQuery.data.cloud_status} cloudName="bambu" />
                  <CloudStatusBanner status={presetsQuery.data.orca_cloud_status} cloudName="orca" />
                </div>
                <button
                  type="button"
                  onClick={handleRefreshPresets}
                  disabled={isRefreshing || isEnqueuing}
                  className="flex-shrink-0 inline-flex items-center gap-1 px-2 py-1 rounded-md text-xs text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary/40 disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
                  title={t('slice.refreshPresetsTitle')}
                  aria-label={t('slice.refreshPresets')}
                >
                  <RefreshCw className={`w-3.5 h-3.5 ${isRefreshing ? 'animate-spin' : ''}`} />
                  {t('slice.refreshPresets')}
                </button>
              </div>
              {/* Slicer picker — two big card-buttons matching the
                  "Filament Tracking" pattern in Settings. Each card carries
                  its own live health status (version / offline / checking)
                  pulled from the same shared React Query cache. Always
                  renders so the user sees what's available even when only
                  one sidecar is up; offline ones are disabled and can't
                  be picked. */}
              <SlicerPicker
                value={pickedSlicer}
                onChange={setPickedSlicer}
                disabled={isEnqueuing}
              />
              {/* Slicer Pipelines (#1425): apply a saved preset bundle to all
                  four slots, or save the current selection as a pipeline.
                  Pipelines are managed in Settings → Pipelines. */}
              <div className="flex flex-wrap items-center gap-2 px-2 py-1.5 rounded-md bg-bambu-dark/40 border border-bambu-dark-tertiary">
                <span className="text-xs font-medium text-bambu-gray flex items-center gap-1">
                  <Cog className="w-3.5 h-3.5" /> {t('slice.pipelines.label', 'Pipeline')}
                </span>
                <select
                  value=""
                  disabled={isEnqueuing || (pipelinesQuery.data?.pipelines.length ?? 0) === 0}
                  onChange={(e) => {
                    const id = parseInt(e.target.value, 10);
                    if (Number.isNaN(id)) return;
                    const picked = pipelinesQuery.data?.pipelines.find((p) => p.id === id);
                    if (!picked) return;
                    // Apply slot state. The filament list is right-padded from
                    // current state so a pipeline with fewer entries than the
                    // current source's slot count keeps the existing tail.
                    setPrinterPreset(picked.printer_preset);
                    setProcessPreset(picked.process_preset);
                    if (picked.bed_type) setBedType(picked.bed_type as BedType);
                    setFilamentPresets((current) => {
                      const next = current.length > 0 ? [...current] : picked.filament_presets.map(() => null as PresetRef | null);
                      for (let i = 0; i < next.length; i++) {
                        if (i < picked.filament_presets.length) {
                          next[i] = picked.filament_presets[i];
                        }
                      }
                      return next;
                    });
                    showToast(t('slice.pipelines.toast.applied', 'Applied "{{name}}"', { name: picked.name }), 'success');
                    // Reset the dropdown so the user can re-apply the same
                    // pipeline if needed (selects don't fire onChange when
                    // value reselects the same option).
                    e.target.value = '';
                  }}
                  className="text-xs px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white disabled:opacity-50 disabled:cursor-not-allowed flex-1 min-w-[10ch]"
                  aria-label={t('slice.pipelines.applyAria', 'Apply pipeline')}
                >
                  <option value="">
                    {(pipelinesQuery.data?.pipelines.length ?? 0) === 0
                      ? t('slice.pipelines.empty', 'No saved pipelines')
                      : t('slice.pipelines.applyPrompt', 'Apply pipeline…')}
                  </option>
                  {pipelinesQuery.data?.pipelines.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
                </select>
                {!savePipelineOpen ? (
                  <button
                    type="button"
                    onClick={() => {
                      setPipelineDraftName('');
                      setSavePipelineOpen(true);
                    }}
                    disabled={
                      isEnqueuing ||
                      !printerPreset ||
                      !processPreset ||
                      filamentPresets.length === 0 ||
                      filamentPresets.some((f) => f === null)
                    }
                    className="text-xs px-2 py-1 bg-bambu-green/20 hover:bg-bambu-green/30 text-bambu-green border border-bambu-green/40 rounded disabled:opacity-50 disabled:cursor-not-allowed"
                    title={t('slice.pipelines.saveTitle', 'Save the current four-slot selection as a reusable pipeline')}
                  >
                    {t('slice.pipelines.saveButton', 'Save as pipeline')}
                  </button>
                ) : (
                  <div className="flex items-center gap-1 flex-1 min-w-[16ch]">
                    <input
                      autoFocus
                      value={pipelineDraftName}
                      onChange={(e) => setPipelineDraftName(e.target.value)}
                      placeholder={t('slice.pipelines.namePlaceholder', 'Pipeline name')}
                      aria-label={t('slice.pipelines.nameAria', 'New pipeline name')}
                      className="flex-1 text-xs px-2 py-1 bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
                    />
                    <button
                      type="button"
                      onClick={() => {
                        const trimmed = pipelineDraftName.trim();
                        if (!trimmed || !printerPreset || !processPreset) return;
                        const nonNull = filamentPresets.filter((f): f is PresetRef => f !== null);
                        if (nonNull.length === 0) return;
                        createPipelineMutation.mutate({
                          name: trimmed,
                          printer_preset: printerPreset,
                          process_preset: processPreset,
                          filament_presets: nonNull,
                          bed_type: bedType,
                        });
                      }}
                      disabled={createPipelineMutation.isPending || !pipelineDraftName.trim()}
                      className="text-xs px-2 py-1 bg-bambu-green hover:bg-bambu-green/80 text-white rounded disabled:opacity-50"
                    >
                      {createPipelineMutation.isPending ? (
                        <Loader2 className="w-3 h-3 animate-spin" />
                      ) : (
                        t('common.save', 'Save')
                      )}
                    </button>
                    <button
                      type="button"
                      onClick={() => {
                        setSavePipelineOpen(false);
                        setPipelineDraftName('');
                      }}
                      className="text-xs px-2 py-1 text-bambu-gray hover:text-white"
                    >
                      {t('common.cancel', 'Cancel')}
                    </button>
                  </div>
                )}
              </div>
              {/* Bed plate picker — five values from BambuStudio's
                  ``curr_bed_type`` enum. Always sent on slice (the slicer
                  CLI's silent fallback to "Cool Plate" is the bug we're
                  fixing for STL / pure-3MF inputs). Default Textured PEI
                  matches the factory plate on X1C / P1S / H2D; A1 owners
                  flip to SuperTack once and localStorage persists. */}
              <BedTypePicker value={bedType} onChange={setBedType} disabled={isEnqueuing} />
              {/* Preset-source control: 3-state owner filter (All / My
                  Presets / Built-in) applied across the cloud / local /
                  standard tiers. */}
              <PresetSourceControl
                ownerFilter={filterOwner}
                onOwnerFilterChange={setFilterOwner}
                disabled={isEnqueuing}
              />
              <PresetDropdown
                label={t('slice.printer', 'Printer profile')}
                slot="printer"
                data={presetsQuery.data}
                value={printerPreset}
                onChange={setPrinterPreset}
                disabled={isEnqueuing}
                ownerFilter={filterOwner}
              />
              <PresetDropdown
                label={t('slice.process', 'Process profile')}
                slot="process"
                data={presetsQuery.data}
                value={processPreset}
                onChange={setProcessPreset}
                disabled={isEnqueuing}
                ownerFilter={filterOwner}
                selectedPrinterName={selectedPrinterName}
                compatIndex={compatIndex}
              />
              {/* Filament reqs may need a server-side preview-slice for
                  unsliced project files (single-pass, then cached). Show a
                  scoped spinner so the user sees the printer/process
                  dropdowns instead of an opaque "Loading presets…" wait. */}
              {filamentReqsQuery.isLoading ? (
                <FilamentAnalysisSpinner
                  requestId={previewRequestId}
                  sourceName={source.filename}
                />
              ) : (
                filamentSlots.map((slot, idx) => {
                  // Slots flagged by the backend as not used by the
                  // picked plate are auto-picked from project metadata
                  // and disabled — the slicer CLI still needs a
                  // profile per project slot, but the user shouldn't
                  // have to think about slots their plate doesn't
                  // paint with. used_in_plate defaults to true when
                  // missing (sliced 3MFs and the no-flag legacy path).
                  const isUsed = slot.used_in_plate !== false;
                  const baseLabel =
                    filamentSlots.length > 1
                      ? t('slice.filamentSlot', {
                          index: idx + 1,
                          type: slot.type,
                          defaultValue: `Filament ${idx + 1} (${slot.type || ''})`,
                        })
                      : t('slice.filament', 'Filament profile');
                  const label = isUsed
                    ? baseLabel
                    : `${baseLabel} ${t('slice.notUsedByPlate', '— not used by this plate')}`;
                  return (
                    <PresetDropdown
                      key={`filament-${idx}`}
                      label={label}
                      slot="filament"
                      data={presetsQuery.data}
                      value={filamentPresets[idx] ?? null}
                      onChange={(ref) =>
                        setFilamentPresets((current) => {
                          const next = current.length === filamentSlots.length
                            ? [...current]
                            : filamentSlots.map((_, i) => current[i] ?? null);
                          next[idx] = ref;
                          return next;
                        })
                      }
                      disabled={isEnqueuing || !isUsed}
                      swatchColor={filamentSlots.length > 1 ? slot.color : undefined}
                      ownerFilter={filterOwner}
                      selectedPrinterName={selectedPrinterName}
                      compatIndex={compatIndex}
                    />
                  );
                })
              )}
            </>
          )}

          {errorMessage && (
            <div className="text-sm text-red-400 bg-red-900/20 border border-red-900/40 rounded p-2" role="alert">
              {errorMessage}
            </div>
          )}
        </div>

        {/* Footer */}
        <div className="flex-shrink-0 flex justify-end gap-2 px-4 py-3 border-t border-bambu-dark-tertiary/40">
          <button
            type="button"
            onClick={onClose}
            disabled={isEnqueuing}
            className="px-3 py-1.5 text-sm rounded-md border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray transition-colors disabled:opacity-50"
          >
            {t('common.cancel', 'Cancel')}
          </button>
          <button
            type="button"
            onClick={() => {
              setErrorMessage(null);
              enqueueMutation.mutate();
            }}
            disabled={!isReady || isEnqueuing}
            className="px-3 py-1.5 text-sm rounded-md bg-bambu-green hover:bg-bambu-green/90 text-bambu-dark font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {isEnqueuing ? (
              <>
                <Loader2 className="w-4 h-4 animate-spin" />
                {t('slice.enqueuing', 'Submitting slice job…')}
              </>
            ) : (
              t('slice.action', 'Slice')
            )}
          </button>
        </div>
      </div>
    </div>
  );
}

function CloudStatusBanner({
  status,
  cloudName = 'bambu',
}: {
  status: SlicerCloudStatus;
  cloudName?: 'bambu' | 'orca';
}) {
  const { t } = useTranslation();
  // `ok` is the happy path. `not_authenticated` is silenced too: a user who
  // hasn't signed in (or has explicitly logged out — #1712) doesn't need a
  // permanent nag at the top of the modal; sign-in lives on the Profiles
  // page if they want it. Only `expired` and `unreachable` surface — those
  // are real breakage states a previously-signed-in user needs to see.
  if (status === 'ok' || status === 'not_authenticated') return null;

  // Same status vocabulary for both Bambu and Orca Cloud — only the
  // user-facing text varies. The fallbacks below name each cloud explicitly
  // so the banner makes sense without translation when i18n hasn't been
  // updated for a new locale.
  const messages =
    cloudName === 'orca'
      ? {
          expired: {
            key: 'slice.orcaCloud.expired',
            fallback: 'Orca Cloud session expired — sign in again to refresh your Orca presets.',
          },
          unreachable: {
            key: 'slice.orcaCloud.unreachable',
            fallback: 'Orca Cloud is unreachable right now. Other presets still work.',
          },
        }
      : {
          expired: {
            key: 'slice.cloud.expired',
            fallback: 'Bambu Cloud session expired — sign in again to refresh your cloud presets.',
          },
          unreachable: {
            key: 'slice.cloud.unreachable',
            fallback: 'Bambu Cloud is unreachable right now. Local and standard presets still work.',
          },
        };

  const tones: Record<'expired' | 'unreachable', { tone: string; icon: typeof Cloud }> = {
    expired: {
      tone: 'border-amber-700/40 bg-amber-900/20 text-amber-200',
      icon: CloudOff,
    },
    unreachable: {
      tone: 'border-bambu-dark-tertiary/40 bg-bambu-dark text-bambu-gray',
      icon: CloudOff,
    },
  };
  const { tone, icon: Icon } = tones[status];
  const { key, fallback } = messages[status];
  return (
    <div className={`flex items-start gap-2 text-xs rounded-md border p-2 ${tone}`} role="status">
      <Icon className="w-4 h-4 flex-shrink-0 mt-0.5" />
      <span>{t(key, fallback)}</span>
    </div>
  );
}
