/**
 * AuthoredFamiliesSection — management block for authored filament families
 * (the spec-B wiring that waited on the Orca write leg, 2026-08-24): explicit
 * push / re-push per cloud, the Orca conflict dialog (force / adopt — the
 * user's explicit call, never automatic), and family deletion with an
 * optional cloud sweep. Renders nothing while no authored family exists.
 */
import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { CloudUpload, Loader2, Trash2, X } from 'lucide-react';
import { api } from '../api/client';
import type { AuthoredFamily, FamilyPushResult } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

interface ConflictState {
  familyId: string;
  rows: FamilyPushResult[];
}

export function AuthoredFamiliesSection() {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();

  const { data } = useQuery({
    queryKey: ['authored-families'],
    queryFn: api.getAuthoredFamilies,
  });
  const { data: options } = useQuery({
    queryKey: ['filament-authoring-options'],
    queryFn: () => api.getFilamentAuthoringOptions(),
    staleTime: 5 * 60_000,
  });
  const { data: cloudStatus } = useQuery({
    queryKey: ['cloud-status'],
    queryFn: () => api.getCloudStatus(),
    staleTime: 60_000,
  });
  const { data: orcaStatus } = useQuery({
    queryKey: ['orca-cloud-status'],
    queryFn: () => api.orcaCloudStatus(),
    staleTime: 60_000,
  });

  const [conflicts, setConflicts] = useState<ConflictState | null>(null);
  const [deleting, setDeleting] = useState<AuthoredFamily | null>(null);
  const [alsoCloud, setAlsoCloud] = useState(false);

  const bambuReady = !!cloudStatus?.is_authenticated && !!options?.push?.bambu;
  const orcaReady =
    !!orcaStatus?.connected && !!options?.push?.orca && (orcaStatus?.scope || '').includes('sync:write');

  const refresh = () => queryClient.invalidateQueries({ queryKey: ['authored-families'] });

  const pushMutation = useMutation({
    mutationFn: ({ familyId, ecosystem }: { familyId: string; ecosystem: 'bambu' | 'orca' }) =>
      api.pushFilamentFamily(familyId, ecosystem),
    onSuccess: ({ results }, { familyId }) => {
      refresh();
      const conflictRows = results.filter((r) => r.status === 'conflict');
      const errors = results.filter((r) => r.status === 'error');
      const ok = results.length - conflictRows.length - errors.length;
      if (conflictRows.length > 0) setConflicts({ familyId, rows: conflictRows });
      if (errors.length > 0) {
        showToast(`${t('authoring.families.pushErrors', { count: errors.length })}: ${errors[0].detail || ''}`, 'error');
      } else if (conflictRows.length === 0) {
        showToast(t('authoring.families.pushDone', { count: ok }));
      }
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const resolveMutation = useMutation({
    mutationFn: ({ familyId, rowId, action }: { familyId: string; rowId: number; action: 'force' | 'adopt' }) =>
      api.resolvePushFamilyConflict(familyId, rowId, action),
    onSuccess: (out, { rowId }) => {
      refresh();
      showToast(out.status === 'adopted' ? t('authoring.families.adopted') : t('authoring.families.overwritten'));
      setConflicts((prev) => {
        if (!prev) return null;
        const rows = prev.rows.filter((r) => r.row_id !== rowId);
        return rows.length > 0 ? { ...prev, rows } : null;
      });
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: ({ familyId, cloud }: { familyId: string; cloud: boolean }) =>
      api.deleteFilamentFamily(familyId, cloud),
    onSuccess: () => {
      refresh();
      queryClient.invalidateQueries({ queryKey: ['localPresets'] });
      showToast(t('authoring.families.deleted'));
      setDeleting(null);
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const families = data?.families || [];
  if (families.length === 0) return null;

  const pushState = (family: AuthoredFamily, eco: 'bambu' | 'orca') => {
    const pushed = family.presets.filter((p) => (eco === 'bambu' ? p.bambu_pushed_id : p.orca_profile_id)).length;
    const dirty = family.presets.filter((p) => (eco === 'bambu' ? p.bambu_dirty : p.orca_dirty)).length;
    if (pushed === 0) return t('authoring.families.notPushed');
    if (dirty > 0) return t('authoring.families.dirty', { count: dirty });
    return t('authoring.families.pushed', { count: pushed });
  };

  return (
    <div className="space-y-2" data-testid="authored-families">
      <h3 className="text-sm font-medium text-white">{t('authoring.families.title')}</h3>
      {families.map((family) => (
        <div
          key={family.filament_id}
          className="flex flex-wrap items-center gap-2 rounded-lg border border-bambu-dark-tertiary bg-bambu-dark-secondary px-3 py-2"
        >
          <div className="min-w-0 flex-1">
            <p className="truncate text-sm text-white">{family.alias || family.filament_id}</p>
            <p className="text-xs text-bambu-gray">
              {family.filament_type} · Bambu: {pushState(family, 'bambu')} · Orca: {pushState(family, 'orca')}
            </p>
          </div>
          <Button
            variant="secondary"
            size="sm"
            disabled={!bambuReady || pushMutation.isPending}
            title={!bambuReady ? t('authoring.cloudRequired') : undefined}
            onClick={() => pushMutation.mutate({ familyId: family.filament_id, ecosystem: 'bambu' })}
          >
            <CloudUpload className="w-3.5 h-3.5" />
            Bambu
          </Button>
          <Button
            variant="secondary"
            size="sm"
            disabled={!orcaReady || pushMutation.isPending}
            title={!orcaReady ? t('authoring.orcaNeedsWrite') : undefined}
            onClick={() => pushMutation.mutate({ familyId: family.filament_id, ecosystem: 'orca' })}
          >
            <CloudUpload className="w-3.5 h-3.5" />
            Orca
          </Button>
          <button
            type="button"
            onClick={() => {
              setAlsoCloud(false);
              setDeleting(family);
            }}
            className="p-1.5 rounded hover:bg-bambu-dark-tertiary text-bambu-gray hover:text-red-400 transition-colors"
            aria-label={t('authoring.families.delete')}
            title={t('authoring.families.delete')}
          >
            <Trash2 className="w-4 h-4" />
          </button>
        </div>
      ))}

      {conflicts && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4">
          <div className="bg-bambu-dark border border-bambu-dark-tertiary rounded-xl w-full max-w-md shadow-2xl">
            <div className="flex items-center justify-between p-4 border-b border-bambu-dark-tertiary">
              <h2 className="text-lg font-semibold text-white">{t('authoring.families.conflictTitle')}</h2>
              <button type="button" onClick={() => setConflicts(null)} className="text-bambu-gray hover:text-white">
                <X className="w-5 h-5" />
              </button>
            </div>
            <div className="p-4 space-y-3">
              <p className="text-sm text-bambu-gray">{t('authoring.families.conflictBody')}</p>
              {conflicts.rows.map((row) => (
                <div
                  key={row.row_id}
                  className="rounded-lg border border-yellow-700/60 bg-yellow-500/10 p-3 space-y-2"
                  data-testid="push-conflict-row"
                >
                  <p className="text-sm text-white">{row.name}</p>
                  <div className="flex gap-2">
                    <Button
                      size="sm"
                      disabled={resolveMutation.isPending}
                      onClick={() =>
                        row.row_id != null &&
                        resolveMutation.mutate({ familyId: conflicts.familyId, rowId: row.row_id, action: 'force' })
                      }
                    >
                      {t('authoring.families.overwriteCloud')}
                    </Button>
                    <Button
                      variant="secondary"
                      size="sm"
                      disabled={resolveMutation.isPending}
                      onClick={() =>
                        row.row_id != null &&
                        resolveMutation.mutate({ familyId: conflicts.familyId, rowId: row.row_id, action: 'adopt' })
                      }
                    >
                      {t('authoring.families.adoptCloud')}
                    </Button>
                  </div>
                </div>
              ))}
              {resolveMutation.isPending && <Loader2 className="w-4 h-4 animate-spin text-bambu-gray" />}
            </div>
          </div>
        </div>
      )}

      {deleting && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 backdrop-blur-sm p-4">
          <div className="bg-bambu-dark border border-bambu-dark-tertiary rounded-xl w-full max-w-sm shadow-2xl">
            <div className="p-4 space-y-3">
              <p className="text-sm text-white">
                {t('authoring.families.deleteConfirm', { name: deleting.alias || deleting.filament_id })}
              </p>
              <label className="flex items-center gap-2 text-sm text-white">
                <input type="checkbox" checked={alsoCloud} onChange={(e) => setAlsoCloud(e.target.checked)} />
                {t('authoring.families.alsoCloud')}
              </label>
              <div className="flex justify-end gap-2">
                <Button variant="secondary" onClick={() => setDeleting(null)}>
                  {t('common.cancel')}
                </Button>
                <Button
                  variant="danger"
                  disabled={deleteMutation.isPending}
                  onClick={() => deleteMutation.mutate({ familyId: deleting.filament_id, cloud: alsoCloud })}
                >
                  {t('common.delete')}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
