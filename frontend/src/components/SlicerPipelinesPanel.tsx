import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, Check, Edit2, Loader2, Search, Trash2, Workflow, X } from 'lucide-react';
import {
  api,
  type PresetRef,
  type PresetSource,
  type SlicerPipeline,
  type UnifiedPresetsResponse,
} from '../api/client';
import { resolvePresetName } from './preset-picker/presetPickerUtils';
import { Card, CardContent, CardHeader } from './Card';
import { useToast } from '../contexts/ToastContext';

// Saved slice preset bundles — CRUD only. A bundle is created from the Slice
// dialog ("Save as pipeline"); this panel renames / re-describes / deletes
// them and shows what each one actually points at, including refs whose
// preset has since disappeared from the slicer catalogue.

const SOURCE_LABEL: Record<PresetSource, string> = {
  orca_cloud: 'Orca Cloud',
  cloud: 'Bambu Cloud',
  local: 'Imported',
  standard: 'Standard',
};

export function SlicerPipelinesPanel() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();

  const { data: list, isLoading, error } = useQuery({
    queryKey: ['slicer-pipelines'],
    queryFn: () => api.listSlicerPipelines(),
  });

  // The unified presets endpoint is the source of pretty names for each
  // PresetRef. Same query key the SliceModal uses, so opening the Slice
  // dialog after visiting this tab (or vice versa) reuses one cached listing
  // instead of round-tripping the slicer registry twice.
  const { data: presets } = useQuery({
    queryKey: ['slicerPresets'],
    queryFn: () => api.getSlicerPresets(),
    staleTime: 60_000,
  });

  const updateMutation = useMutation({
    mutationFn: ({
      id,
      name,
      description,
    }: {
      id: number;
      name?: string;
      description?: string | null;
    }) => api.updateSlicerPipeline(id, { name, description }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slicer-pipelines'] });
      showToast(t('settings.pipelines.toast.saved', 'Pipeline saved'), 'success');
    },
    onError: (err: Error) => {
      showToast(err.message || t('settings.pipelines.toast.saveFailed', 'Save failed'), 'error');
    },
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteSlicerPipeline(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['slicer-pipelines'] });
      showToast(t('settings.pipelines.toast.deleted', 'Pipeline deleted'), 'success');
    },
    onError: (err: Error) => {
      showToast(err.message || t('settings.pipelines.toast.deleteFailed', 'Delete failed'), 'error');
    },
  });

  // Panel-level search by bundle name (case-insensitive substring). State is
  // local — the list is small enough that re-rendering on every keystroke is
  // fine.
  const [searchTerm, setSearchTerm] = useState('');

  const allPipelines = useMemo(() => list?.pipelines ?? [], [list?.pipelines]);

  const pipelines = useMemo(() => {
    const term = searchTerm.trim().toLowerCase();
    if (!term) return allPipelines;
    return allPipelines.filter((p) => p.name.toLowerCase().includes(term));
  }, [allPipelines, searchTerm]);

  return (
    <Card>
      <CardHeader>
        <h3 className="text-base font-semibold text-white flex items-center gap-2">
          <Workflow className="w-4 h-4 text-bambu-green" />
          {t('settings.pipelines.title', 'Slicer Pipelines')}
        </h3>
        <p className="text-xs text-bambu-gray mt-1">
          {t(
            'settings.pipelines.subtitle',
            'Reusable preset bundles (printer + process + filaments + bed type). Save one from the Slice dialog and apply it with a single click on the next file.',
          )}
        </p>
      </CardHeader>
      <CardContent>
        {isLoading && (
          <div className="flex items-center gap-2 text-sm text-bambu-gray">
            <Loader2 className="w-4 h-4 animate-spin" />
            {t('settings.pipelines.loading', 'Loading pipelines…')}
          </div>
        )}
        {error && (
          <div className="text-sm text-red-700 dark:text-red-400">
            {t('settings.pipelines.loadError', 'Could not load pipelines.')}
          </div>
        )}
        {/* Search. Only render when there is something to filter; the
            empty-state hint reads better without controls above it. */}
        {!isLoading && !error && allPipelines.length > 0 && (
          <div className="flex flex-wrap items-center gap-2 mb-3">
            <div className="relative flex-1 min-w-[12rem]">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-bambu-gray pointer-events-none" />
              <input
                type="search"
                value={searchTerm}
                onChange={(e) => setSearchTerm(e.target.value)}
                placeholder={t('settings.pipelines.searchPlaceholder', 'Search pipelines…')}
                aria-label={t('settings.pipelines.searchPlaceholder', 'Search pipelines…')}
                className="w-full pl-7 pr-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
              />
            </div>
            {searchTerm && (
              <span className="text-xs text-bambu-gray">
                {t('settings.pipelines.filter.count', '{{shown}} / {{total}}', {
                  shown: pipelines.length,
                  total: allPipelines.length,
                })}
              </span>
            )}
          </div>
        )}
        {!isLoading && !error && allPipelines.length === 0 && (
          <div className="text-sm text-bambu-gray space-y-2">
            <p>{t('settings.pipelines.empty.title', 'No pipelines yet.')}</p>
            <p>
              {t(
                'settings.pipelines.empty.howto',
                'Open the Slice dialog for any file, pick your printer / process / filaments / bed type, then click "Save as pipeline". Your saved pipelines will appear here.',
              )}
            </p>
          </div>
        )}
        {!isLoading && !error && allPipelines.length > 0 && pipelines.length === 0 && (
          <p className="text-sm text-bambu-gray">
            {t('settings.pipelines.filter.noMatches', 'No pipelines match the current filters.')}
          </p>
        )}
        {!isLoading && !error && pipelines.length > 0 && (
          <div className="space-y-2">
            {pipelines.map((p) => (
              <PipelineRow
                key={p.id}
                pipeline={p}
                presets={presets}
                onSave={(payload) => updateMutation.mutate({ id: p.id, ...payload })}
                onDelete={() => {
                  if (confirm(t('settings.pipelines.confirmDelete', 'Delete this pipeline? This cannot be undone.'))) {
                    deleteMutation.mutate(p.id);
                  }
                }}
                saving={updateMutation.isPending}
                deleting={deleteMutation.isPending}
              />
            ))}
          </div>
        )}
      </CardContent>
    </Card>
  );
}

function PipelineRow({
  pipeline,
  presets,
  onSave,
  onDelete,
  saving,
  deleting,
}: {
  pipeline: SlicerPipeline;
  presets: UnifiedPresetsResponse | undefined;
  onSave: (payload: { name?: string; description?: string | null }) => void;
  onDelete: () => void;
  saving: boolean;
  deleting: boolean;
}) {
  const { t } = useTranslation();
  const [editing, setEditing] = useState(false);
  const [draftName, setDraftName] = useState(pipeline.name);
  const [draftDescription, setDraftDescription] = useState(pipeline.description ?? '');

  const printerName = resolvePresetName(presets, pipeline.printer_preset, 'printer');
  const processName = resolvePresetName(presets, pipeline.process_preset, 'process');
  const filamentResolutions = pipeline.filament_presets.map((f) =>
    resolvePresetName(presets, f, 'filament'),
  );
  // Collapse identical filaments into a single "All N slots" line — most
  // production bundles load the same filament into every AMS slot, and
  // listing the same line three times is just noise. Compares raw preset
  // refs (source + id) rather than resolved names so the dedup is correct
  // even when ``presets`` hasn't loaded yet.
  const filamentsAllIdentical =
    pipeline.filament_presets.length > 1 &&
    pipeline.filament_presets.every(
      (f) =>
        f.source === pipeline.filament_presets[0].source &&
        f.id === pipeline.filament_presets[0].id,
    );

  const hasStaleRef =
    presets !== undefined &&
    (printerName === null || processName === null || filamentResolutions.some((n) => n === null));

  const handleSave = () => {
    const trimmedName = draftName.trim();
    if (!trimmedName) return;
    onSave({
      name: trimmedName,
      description: draftDescription.trim() || null,
    });
    setEditing(false);
  };

  const handleCancel = () => {
    setDraftName(pipeline.name);
    setDraftDescription(pipeline.description ?? '');
    setEditing(false);
  };

  return (
    <div className="rounded-md border border-bambu-dark-tertiary bg-bambu-dark/40 px-3 py-2">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0 flex-1">
          {editing ? (
            <div className="space-y-2">
              <input
                value={draftName}
                onChange={(e) => setDraftName(e.target.value)}
                aria-label={t('settings.pipelines.field.name', 'Pipeline name')}
                placeholder={t('settings.pipelines.field.name', 'Pipeline name')}
                className="w-full px-2 py-1 text-sm bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
              />
              <textarea
                value={draftDescription}
                onChange={(e) => setDraftDescription(e.target.value)}
                aria-label={t('settings.pipelines.field.description', 'Description')}
                placeholder={t('settings.pipelines.field.description', 'Description')}
                rows={2}
                className="w-full px-2 py-1 text-xs bg-bambu-dark border border-bambu-dark-tertiary rounded text-white"
              />
            </div>
          ) : (
            <>
              <h4 className="text-sm font-medium text-white truncate">{pipeline.name}</h4>
              {pipeline.description && (
                <p className="text-xs text-bambu-gray mt-0.5">{pipeline.description}</p>
              )}
            </>
          )}
        </div>
        <div className="flex items-center gap-1 flex-shrink-0">
          {editing ? (
            <>
              <button
                onClick={handleSave}
                disabled={saving || !draftName.trim()}
                aria-label={t('settings.pipelines.action.save', 'Save')}
                className="p-1.5 text-bambu-green hover:bg-bambu-dark-tertiary rounded disabled:opacity-50"
              >
                <Check className="w-4 h-4" />
              </button>
              <button
                onClick={handleCancel}
                aria-label={t('settings.pipelines.action.cancel', 'Cancel')}
                className="p-1.5 text-bambu-gray hover:bg-bambu-dark-tertiary rounded"
              >
                <X className="w-4 h-4" />
              </button>
            </>
          ) : (
            <>
              <button
                onClick={() => setEditing(true)}
                aria-label={t('settings.pipelines.action.rename', 'Rename')}
                className="p-1.5 text-bambu-gray hover:text-white hover:bg-bambu-dark-tertiary rounded"
              >
                <Edit2 className="w-4 h-4" />
              </button>
              <button
                onClick={onDelete}
                disabled={deleting}
                aria-label={t('settings.pipelines.action.delete', 'Delete')}
                className="p-1.5 text-bambu-gray hover:text-red-600 dark:hover:text-red-400 hover:bg-bambu-dark-tertiary rounded disabled:opacity-50"
              >
                <Trash2 className="w-4 h-4" />
              </button>
            </>
          )}
        </div>
      </div>

      {!editing && (
        <div className="mt-2 grid grid-cols-1 md:grid-cols-2 gap-x-4 gap-y-2 text-xs">
          {/* Profiles group — printer / process / bed. These travel together
              because they describe the slicer profile bundle that produces a
              single gcode. The full preset name (including the BambuStudio
              ``@BBL <model>`` suffix) is shown verbatim so the user can match
              it 1:1 against what they see in the slicer. */}
          <div className="space-y-0.5">
            <div className="text-[10px] uppercase tracking-wide text-bambu-gray/60">
              {t('settings.pipelines.group.profiles', 'Profiles')}
            </div>
            <PresetLine
              label={t('settings.pipelines.slot.printer', 'Printer')}
              presetRef={pipeline.printer_preset}
              name={printerName}
            />
            <PresetLine
              label={t('settings.pipelines.slot.process', 'Process')}
              presetRef={pipeline.process_preset}
              name={processName}
            />
            {pipeline.bed_type && (
              <div className="text-bambu-gray">
                <span className="font-medium text-bambu-gray/80">
                  {t('settings.pipelines.slot.bed', 'Bed')}:
                </span>{' '}
                <span className="text-white">{pipeline.bed_type}</span>
              </div>
            )}
          </div>
          {/* Filaments group — one per AMS slot. When every slot is the same
              filament (the common single-color production-batch case) we
              collapse them into a single ``All 4 slots: PLA Basic`` line. */}
          <div className="space-y-0.5">
            <div className="text-[10px] uppercase tracking-wide text-bambu-gray/60">
              {t('settings.pipelines.group.filaments', 'Filaments')}
              {pipeline.filament_presets.length > 1 && (
                <span className="text-bambu-gray/60 normal-case ml-1">
                  ({pipeline.filament_presets.length})
                </span>
              )}
            </div>
            {filamentsAllIdentical ? (
              <PresetLine
                label={t('settings.pipelines.slot.filamentAll', 'All {{n}} slots', {
                  n: pipeline.filament_presets.length,
                })}
                presetRef={pipeline.filament_presets[0]}
                name={filamentResolutions[0]}
              />
            ) : (
              pipeline.filament_presets.map((f, i) => (
                <PresetLine
                  key={i}
                  label={
                    pipeline.filament_presets.length > 1
                      ? t('settings.pipelines.slot.filamentN', 'Filament {{n}}', { n: i + 1 })
                      : t('settings.pipelines.slot.filament', 'Filament')
                  }
                  presetRef={f}
                  name={filamentResolutions[i]}
                />
              ))
            )}
          </div>
        </div>
      )}

      {hasStaleRef && !editing && (
        <div className="mt-2 flex items-center gap-1.5 text-xs text-amber-700 dark:text-amber-400">
          <AlertTriangle className="w-3.5 h-3.5" />
          {t(
            'settings.pipelines.staleWarning',
            'One or more referenced presets no longer exist. Re-save this pipeline from the Slice dialog to fix.',
          )}
        </div>
      )}
    </div>
  );
}

function PresetLine({
  label,
  presetRef,
  name,
}: {
  label: string;
  presetRef: PresetRef;
  name: string | null;
}) {
  return (
    <div className="text-bambu-gray truncate">
      <span className="font-medium text-bambu-gray/80">{label}:</span>{' '}
      {name ? (
        <span className="text-white">{name}</span>
      ) : (
        <span className="text-amber-700 dark:text-amber-400">
          [{SOURCE_LABEL[presetRef.source]} #{presetRef.id}]
        </span>
      )}
    </div>
  );
}
