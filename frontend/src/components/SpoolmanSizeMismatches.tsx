import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { api } from '../api/client';
import type { SpoolmanSizeMismatch } from '../api/client';
import { Button } from './Button';
import { ConfirmModal } from './ConfirmModal';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { DEFAULT_SPOOL_DISPLAY_TEMPLATE, formatSpoolDisplayName } from '../utils/spoolName';

const grams = (value: number | null) => (value === null ? '—' : String(Math.round(value)));

/**
 * Spoolman spools whose own size differs from their filament's.
 *
 * Every figure is computed from the spool's own size, as Spoolman computes its
 * remaining weight; the old spool form wrote a changed label weight to the
 * filament. Nothing is rewritten for the operator: this lists such spools, and
 * the button settles ONE spool, after a confirmation. Renders nothing while
 * there is nothing to show.
 */
export function SpoolmanSizeMismatches() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { hasPermission } = useAuth();
  const canRead = hasPermission('inventory:read');
  const canUpdate = hasPermission('inventory:update');
  const [pending, setPending] = useState<SpoolmanSizeMismatch | null>(null);

  const { data: rows } = useQuery({
    queryKey: ['spoolman-size-mismatches'],
    queryFn: api.getSpoolmanSizeMismatches,
    enabled: canRead,
    staleTime: 60_000,
  });
  // The same template the spool list names a spool with.
  const { data: preferences } = useQuery({
    queryKey: ['ui-preferences'],
    queryFn: api.getUiPreferences,
    staleTime: 60_000,
  });
  const template = preferences?.spool_display_template || DEFAULT_SPOOL_DISPLAY_TEMPLATE;
  const nameOf = (row: SpoolmanSizeMismatch) => formatSpoolDisplayName(row.spool, template) || `#${row.id}`;

  const applyMutation = useMutation({
    mutationFn: (row: SpoolmanSizeMismatch) => api.applyFilamentSizeToSpoolmanSpool(row.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['spoolman-size-mismatches'] });
      queryClient.invalidateQueries({ queryKey: ['spoolman-inventory-spools'] });
      showToast(t('settings.sizeMismatchDone'), 'success');
      setPending(null);
    },
    onError: (error: Error) => {
      showToast(error.message || t('settings.sizeMismatchError'), 'error');
      setPending(null);
    },
  });

  if (!rows || rows.length === 0) return null;

  return (
    <div className="mt-4 pt-4 border-t border-bambu-dark-tertiary space-y-2">
      <div>
        <p className="text-sm text-white">{t('settings.sizeMismatchTitle')}</p>
        <p className="text-xs text-bambu-gray">{t('settings.sizeMismatchDesc')}</p>
      </div>
      <ul className="max-h-64 overflow-y-auto divide-y divide-bambu-dark-tertiary border border-bambu-dark-tertiary rounded">
        {rows.map((row) => (
          <li key={row.id} className="flex flex-wrap items-center justify-between gap-2 px-2 py-1.5">
            <div className="min-w-0">
              <p className="text-sm text-white break-words">
                <span className="text-bambu-gray">#{row.id}</span> {nameOf(row)}
              </p>
              <p className="text-xs text-bambu-gray">
                {t('settings.sizeMismatchRow', {
                  own: grams(row.initial_weight),
                  filament: grams(row.filament_weight),
                  remaining: grams(row.remaining_weight),
                })}
              </p>
            </div>
            {canUpdate && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => setPending(row)}
                disabled={applyMutation.isPending}
                className="flex-shrink-0"
              >
                {t('settings.sizeMismatchUse')}
              </Button>
            )}
          </li>
        ))}
      </ul>

      {pending && (
        <ConfirmModal
          title={t('settings.sizeMismatchConfirmTitle')}
          message={t('settings.sizeMismatchConfirm', {
            name: nameOf(pending),
            id: pending.id,
            own: grams(pending.initial_weight),
            filament: grams(pending.filament_weight),
          })}
          confirmText={t('settings.sizeMismatchConfirmButton', { filament: grams(pending.filament_weight) })}
          variant="warning"
          isLoading={applyMutation.isPending}
          onConfirm={() => applyMutation.mutate(pending)}
          onCancel={() => setPending(null)}
        />
      )}
    </div>
  );
}
