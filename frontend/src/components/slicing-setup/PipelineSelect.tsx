// The saved-set select of the slicing-setup family (spec slicing-setup r5,
// R11, §9): an action list (its value is always empty, so re-picking the same
// set applies it again), the applied / unresolved notes the place passes, and
// — only where the place passes `save` and the person holds both pipeline
// rights — "Save these settings". Absent, and no request, without
// pipelines:read. The markup is the Slice dialog's pipeline block.
import { useId, useState } from 'react';
import { Loader2, AlertTriangle } from 'lucide-react';
import { useQuery } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';

import { api, type SlicerPipeline } from '../../api/client';
import { useAuth } from '../../contexts/AuthContext';
import { Select } from '../Select';

interface PipelineSelectProps {
  onApply: (set: SlicerPipeline) => void;
  filterSets?: (set: SlicerPipeline) => boolean;
  save?: { onSave: (name: string) => void; pending: boolean; canSave: boolean };
  disabled?: boolean;
  applied?: { name: string; changed: string[] } | null;
  unresolved?: string[];
}

export function PipelineSelect({ onApply, filterSets, save, disabled, applied, unresolved }: PipelineSelectProps) {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const canRead = hasPermission('pipelines:read');
  const canWrite = canRead && hasPermission('pipelines:write');
  const selectId = useId();
  const [saveOpen, setSaveOpen] = useState(false);
  const [draftName, setDraftName] = useState('');
  const query = useQuery({
    queryKey: ['slicer-pipelines'],
    queryFn: () => api.listSlicerPipelines(),
    staleTime: 60_000,
    enabled: canRead,
  });
  if (!canRead) return null;
  const sets = (query.data?.pipelines ?? []).filter((s) => !filterSets || filterSets(s));

  return (
    <div>
      <div className="flex items-end justify-between gap-2 mb-1">
        <label htmlFor={selectId} className="text-xs text-bambu-gray">
          {t('slice.pipelines.label', 'Pipeline')}
        </label>
        {save && canWrite && !saveOpen && (
          <button
            type="button"
            onClick={() => {
              setDraftName('');
              setSaveOpen(true);
            }}
            disabled={disabled || !save.canSave}
            className="text-xs text-bambu-green hover:text-bambu-green/80 transition-colors disabled:opacity-50 disabled:cursor-not-allowed"
            title={t('slice.pipelines.saveTitle', 'Save the current four-slot selection as a reusable pipeline')}
          >
            {t('slice.pipelines.saveButton', 'Save as pipeline')}
          </button>
        )}
      </div>
      <Select
        className="w-full"
        id={selectId}
        value=""
        disabled={disabled || sets.length === 0}
        onChange={(e) => {
          const id = Number.parseInt(e.target.value, 10);
          // Reset at once, so picking the same set again applies it again.
          e.target.value = '';
          if (Number.isNaN(id)) return;
          const picked = sets.find((s) => s.id === id);
          if (picked) onApply(picked);
        }}
      >
        <option value="">
          {sets.length === 0 ? t('slice.pipelines.empty', 'No saved pipelines') : t('slice.pipelines.applyPrompt', 'Apply pipeline…')}
        </option>
        {sets.map((s) => (
          <option key={s.id} value={s.id}>
            {s.name}
          </option>
        ))}
      </Select>
      {save && saveOpen && (
        <div className="flex items-center gap-2 mt-2">
          <input
            autoFocus
            value={draftName}
            onChange={(e) => setDraftName(e.target.value)}
            placeholder={t('slice.pipelines.namePlaceholder', 'Pipeline name')}
            aria-label={t('slice.pipelines.nameAria', 'New pipeline name')}
            className="flex-1 min-w-0 px-3 py-2 rounded-md bg-bambu-dark border border-bambu-dark-tertiary text-white text-sm focus:outline-none focus:border-bambu-gray"
          />
          <button
            type="button"
            onClick={() => {
              const trimmed = draftName.trim();
              if (trimmed) save.onSave(trimmed);
            }}
            disabled={save.pending || !draftName.trim()}
            className="flex-shrink-0 px-3 py-1.5 text-sm rounded-md bg-bambu-green hover:bg-bambu-green/90 text-white font-medium transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
          >
            {save.pending && <Loader2 className="w-4 h-4 animate-spin" />}
            {t('common.save', 'Save')}
          </button>
          <button
            type="button"
            onClick={() => {
              setSaveOpen(false);
              setDraftName('');
            }}
            className="flex-shrink-0 px-3 py-1.5 text-sm rounded-md border border-bambu-dark-tertiary text-bambu-gray hover:text-white hover:border-bambu-gray transition-colors"
          >
            {t('common.cancel', 'Cancel')}
          </button>
        </div>
      )}
      {unresolved && unresolved.length > 0 && (
        <div
          className="flex items-start gap-2 text-xs rounded-md border p-2 mt-2 border-amber-300 dark:border-amber-700/40 bg-amber-50 dark:bg-amber-900/20 text-amber-800 dark:text-amber-200"
          role="status"
        >
          <AlertTriangle className="w-4 h-4 flex-shrink-0 mt-0.5" />
          <span>
            {t(
              'slice.pipelines.staleWarning',
              'Some presets saved in this pipeline no longer exist. These stayed on their current selection: {{slots}}',
              { slots: unresolved.join(', ') },
            )}
          </span>
        </div>
      )}
      {applied && (
        <p role="status" className="mt-2 text-xs text-bambu-gray">
          {applied.changed.length > 0
            ? t('slice.pipelines.appliedChanged', {
                name: applied.name,
                fields: applied.changed.join(', '),
                defaultValue: 'Applied "{{name}}". Changed: {{fields}}',
              })
            : t('slice.pipelines.appliedUnchanged', {
                name: applied.name,
                defaultValue: 'Applied "{{name}}". Nothing changed — these were already the selection.',
              })}
        </p>
      )}
    </div>
  );
}
